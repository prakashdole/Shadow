"""Opt-in navigation. Agent-browser is the only browser tool. Phase 1 asks which rows are unread, then Python orders them."""

from __future__ import annotations

from dataclasses import dataclass
import re
from time import monotonic, sleep
from urllib.parse import urlsplit

from shadow.agent_session import AgentError
from shadow.gemini_client import ProviderFailure
from shadow.nav_observe import Bound, Spans, input_tokens, select_target
from shadow.screenshot_loop import (
    MIN_CONFIDENCE, TASK_CLOSE, TASK_REEL, TASK_ROW, _done, thread_id_of,
)
from shadow.targets import classify
from shadow.viewport import HEIGHT, WIDTH, label, open_png, png_bytes


UNREAD_ASK = (
    "Which offered inbox rows are unread? Use the unread dot or an explicit unread label. "
    "A preview, a display name, or a timestamp does not make a row unread. "
    "Choose the set of offered rows that are unread. If any offered row is unclear, hold."
)
REEL_LOOK = (
    "Does this reel belong to the latest incoming turn? Use the sender side, the nearby caption, "
    "and the surrounding messages. The latest incoming turn is the newest incoming message. "
    "Hold for an older incoming reel, an outgoing reel, a photo, a link, or more than one plausible reel."
)
_CONTROL = re.compile(r'(button|link) "([^"]*)" \[ref=(e\d+)\]')
_AGE = re.compile(r"\b(\d+)\s*([mhd])\b", re.I)
_UNREAD = re.compile(r"\bunread\b", re.I)
_HREF = re.compile(r"/direct/t/([0-9]+)")
_UNIT = {"m": 1, "h": 60, "d": 1440}
_HOLD = "Hold. The right control is missing, covered, ambiguous, or unsafe."
_WAIT = "Wait for a fresh screenshot. The screen is still loading."


class Control:
    def __init__(self, ref, role, name):
        self.ref, self.role, self.name = ref, role, name
        self.box = None
        self.href = None
        self.choice = ""
        self.thread = None

    @property
    def center(self):
        left, top, right, bottom = self.box
        return ((left + right) / 2, (top + bottom) / 2)


@dataclass(frozen=True)
class Choice:
    choice: str
    confidence: float
    trace: dict


def age_minutes(label):
    match = _AGE.search(label or "")
    if not match:
        return None
    return int(match.group(1)) * _UNIT[match.group(2).lower()]


def list_order(snapshot):
    text = (snapshot or "").lower()
    newest, oldest = "newest at top" in text, "oldest at top" in text
    if newest == oldest:
        return "newest" if newest else None
    return "newest" if newest else "oldest"


def choose_oldest(rows, order):
    """Ordering rule. None means the evidence does not identify one row."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    ages = [age_minutes(row.name) for row in rows]
    if all(age is not None for age in ages):
        oldest = max(ages)
        tied = [row for row, age in zip(rows, ages) if age == oldest]
        return tied[0] if len(tied) == 1 else max(tied, key=lambda row: row.center[1])
    if any(age is not None for age in ages):
        return None
    if order == "newest":
        return max(rows, key=lambda row: row.center[1])
    if order == "oldest":
        return min(rows, key=lambda row: row.center[1])
    return None


def subset_key(members):
    return "none" if not members else "+".join(members)


def subset_choices(keys):
    choices = {"none": "No offered row is unread."}
    for mask in range(1, 1 << len(keys)):
        members = [keys[index] for index in range(len(keys)) if mask >> index & 1]
        choices[subset_key(members)] = "Unread: " + ", ".join(members) + ". Every other offered row is read."
    choices["hold"] = "The unread status of an offered row is unclear."
    choices["wait"] = _WAIT
    return choices


def parse_members(choice, keys):
    if choice in ("hold", "wait"):
        return choice
    if choice == "none":
        return []
    members = choice.split("+")
    ordered = [key for key in keys if key in members]
    if members and ordered == members and len(members) == len(set(members)):
        return members
    return None


def context_crop(card, side):
    left, top, right, bottom = card
    pad_left = 110 if side != "out" else 12
    pad_right = 12 if side != "out" else 110
    box = (max(0, int(left) - pad_left), max(0, int(top) - 64),
           min(WIDTH, int(right) + pad_right), min(HEIGHT, int(bottom) + 78))
    if box[2] - box[0] < 40 or box[3] - box[1] < 40 or box == (0, 0, WIDTH, HEIGHT):
        return None
    if not crop_covers(box, card):
        return None
    return box


def crop_covers(crop, card):
    left, top, right, bottom = [int(value) for value in card]
    if not (crop[0] <= left and crop[1] <= top and crop[2] >= right and crop[3] >= bottom):
        return False
    return bottom + 24 > HEIGHT or crop[3] >= bottom + 24


def _ibox(box):
    return tuple(int(round(value)) for value in box)


def _intersects(first, second):
    return not (first[2] <= second[0] or second[2] <= first[0] or first[3] <= second[1] or second[3] <= first[1])


def parse_controls(snapshot):
    return [Control(match.group(3), match.group(1), match.group(2))
            for match in _CONTROL.finditer(snapshot or "")]


def thread_of(href):
    if not href:
        return None
    match = _HREF.search(urlsplit(href).path or str(href))
    return match.group(1) if match else None


def side_of(snapshot, ref):
    side = "in"
    for line in (snapshot or "").splitlines():
        if "Outgoing message" in line:
            side = "out"
        elif "Incoming message" in line:
            side = "in"
        if f"ref={ref}]" in line:
            return side
    return side


def _finish(spans, bag, **fields):
    fields.update(spans.report())
    fields.update(calls=bag["calls"], payload_bytes=bag["payload"], sent=bag["sent"],
                  input_tokens=None if bag["tokens_missing"] or not bag["calls"] else bag["tokens"],
                  second_look=bag["second_look"], recovered=bag["recovered"],
                  wrong_clicks=bag["wrong_clicks"], intercepted=bag["intercepted"],
                  evidence_source="agent-browser")
    return _done(**fields)


def _bag():
    return {"calls": 0, "payload": 0, "tokens": 0, "tokens_missing": False, "sent": False,
            "second_look": False, "recovered": False, "wrong_clicks": 0, "intercepted": False}


def _note(bag, trace):
    bag["calls"] += 1
    bag["sent"] = True
    bag["payload"] += trace.get("payload_bytes") or 0
    tokens = input_tokens(trace.get("usage"))
    if tokens is None:
        bag["tokens_missing"] = True
    else:
        bag["tokens"] += tokens


def _known(exc):
    text = str(exc)
    for token in ("agent_browser_unavailable", "agent_browser_version", "agent_browser_timeout"):
        if token in text:
            return token
    return "browser_detach"


def _enrich(session, controls, want_href):
    kept = []
    for control in controls:
        try:
            control.box = session.box(control.ref)
        except AgentError:
            continue
        if want_href and control.role == "link":
            try:
                control.href = session.attr(control.ref, "href")
            except AgentError:
                control.href = None
            control.thread = thread_of(control.href)
        kept.append(control)
    return kept


def _stable(session, selector, bound_path):
    for _attempt in range(2):
        first = session.snapshot(selector)
        raw = session.screenshot()
        image = open_png(raw)
        if image.size != (WIDTH, HEIGHT):
            session.command(["set", "viewport", "1280", "720", "1"])
            continue
        second = session.snapshot(selector)
        url = session.page_url()
        if bound_path and bound_path not in urlsplit(url).path:
            return "wrong_tab", None
        if first == second:
            return None, (raw, image, first, url)
    return "stale_observation", None


def _poll(session, predicate, timeout, pause):
    deadline = monotonic() + timeout
    while True:
        try:
            found = predicate()
        except AgentError:
            found = None
        if found:
            return found
        if monotonic() >= deadline:
            return None
        pause(0.05)


def _ask(session, vision, spans, bag, image, task, choices, evidence, instructions, transport,
         boundary, original, kind, url, crop=None, card=None):
    trace = {}

    def gate(event):
        event["original"] = original
        event["kind"] = kind
        event["url"] = url
        event["crop"] = crop
        event["card"] = card
        event["source_frame"] = original if kind == "full" else None
        if boundary is not None:
            boundary(event)

    try:
        with spans.model():
            choice, confidence = vision.choose(
                image, task, choices, [], evidence=evidence or None, transport=transport,
                boundary=gate, trace=trace, instructions=instructions)
    finally:
        start, end = trace.get("construct_start"), trace.get("construct_end")
        if start is not None and end is not None:
            spans.rows.append(("request_construction", start, end))
    _note(bag, trace)
    return choice, float(confidence)


def _header_account(session, account):
    if not account:
        return True
    text = session.snapshot("header")
    return re.search(rf"\b{re.escape(account)}\b", text) is not None


def _relocate(session, selector, *, thread=None, name=None):
    controls = _enrich(session, parse_controls(session.snapshot(selector)), want_href=thread is not None)
    if thread is not None:
        hits = [control for control in controls if control.thread == thread]
    else:
        hits = [control for control in controls if control.name == name]
    return hits[0] if len(hits) == 1 else None


def _click(session, ref):
    try:
        session.click(ref)
        return None
    except AgentError as exc:
        return str(exc)


def navigate(session, vision, *, task, procedure="phase1", timeout=3.0, pause=None,
             transport=None, boundary=None, bound_thread=None, bound_path=None, bound_account=None):
    if procedure not in ("baseline", "phase1"):
        raise AgentError("bad_procedure")
    pause = sleep if pause is None else pause
    spans, bag = Spans(), _bag()
    try:
        if bound_account is not None and not _header_account(session, bound_account):
            return _finish(spans, bag, outcome="hold", reason="wrong_account")
        url = session.page_url()
        if bound_path and bound_path not in urlsplit(url).path:
            return _finish(spans, bag, outcome="hold", reason="wrong_tab")
        current = thread_id_of(url)
        if bound_thread and current not in (None, bound_thread):
            return _finish(spans, bag, outcome="hold", reason="wrong_thread")
        if task == TASK_ROW:
            return _row(session, vision, spans, bag, procedure=procedure, timeout=timeout, pause=pause,
                        transport=transport, boundary=boundary, bound_path=bound_path or "/direct/inbox/")
        if task == TASK_REEL:
            return _reel(session, vision, spans, bag, procedure=procedure, timeout=timeout, pause=pause,
                         transport=transport, boundary=boundary)
        if task == TASK_CLOSE:
            return _close(session, vision, spans, bag, timeout=timeout, pause=pause,
                          transport=transport, boundary=boundary)
        return _finish(spans, bag, outcome="hold", reason="bad_task")
    except ProviderFailure as exc:
        outcome = "pending" if exc.code in ("rate_limited", "quota", "timeout", "provider") else "hold"
        return _finish(spans, bag, outcome=outcome, reason=exc.code)
    except TimeoutError:
        return _finish(spans, bag, outcome="hold", reason="timeout")
    except AgentError as exc:
        return _finish(spans, bag, outcome="hold", reason=_known(exc))
    except (OSError, ValueError):
        return _finish(spans, bag, outcome="hold", reason="bad_capture")


def _row(session, vision, spans, bag, *, procedure, timeout, pause, transport, boundary, bound_path):
    with spans.span("snapshot"):
        status, captured = _stable(session, "#threads", bound_path)
    if status:
        return _finish(spans, bag, outcome="hold" if status != "wrong_tab" else "hold", reason=status,
                       stale_rejected=1 if status == "stale_observation" else 0)
    raw, image, snapshot, url = captured
    screen = classify(image, url)
    if screen == "login":
        return _finish(spans, bag, outcome="login", reason="login_required", screen=screen)
    controls = _enrich(session, parse_controls(snapshot), want_href=True)
    rows = [control for control in controls if control.thread]
    rows.sort(key=lambda control: (control.box[1], control.box[0]))
    if not rows:
        reason = screen if screen in ("blank", "header_only", "unknown") else "no_target"
        outcome = "wait" if reason != "no_target" else "wait"
        return _finish(spans, bag, outcome=outcome, reason=reason, screen=screen)
    if procedure == "baseline":
        return _baseline_row(session, vision, spans, bag, raw, image, snapshot, url, rows,
                             timeout=timeout, pause=pause, transport=transport, boundary=boundary)
    for index, control in enumerate(rows, start=1):
        control.choice = f"c{index}"
    if len(rows) > 6:
        return _finish(spans, bag, outcome="hold", reason="too_many", screen=screen)
    keys = [control.choice for control in rows]
    evidence = "\n".join(
        f"{control.choice}: {control.name} | box {','.join(str(value) for value in _ibox(control.box))}"
        for control in rows) + "\n" + snapshot
    with spans.span("encode"):
        encoded = label(image, [(control.choice, _ibox(control.box)) for control in rows])
    choice, confidence = _ask(session, vision, spans, bag, encoded, UNREAD_ASK, subset_choices(keys),
                              evidence[:1500], UNREAD_ASK, transport, boundary, raw, "full", url)
    if choice == "wait":
        return _finish(spans, bag, outcome="wait", reason="wait", screen=screen)
    members = parse_members(choice, keys)
    if members is None or choice == "hold" or confidence < MIN_CONFIDENCE:
        return _finish(spans, bag, outcome="hold",
                       reason="low_confidence" if confidence < MIN_CONFIDENCE else "hold", screen=screen)
    picked = choose_oldest([control for control in rows if control.choice in members], list_order(snapshot))
    if picked is None:
        return _finish(spans, bag, outcome="hold", reason="unordered" if members else "no_unread", screen=screen)
    return _open_row(session, spans, bag, picked, timeout=timeout, pause=pause, screen=screen)


def _baseline_row(session, vision, spans, bag, raw, image, snapshot, url, rows, *, timeout, pause,
                  transport, boundary):
    from shadow.targets import offer
    screen = classify(image, url)
    found = [candidate for candidate in offer(image, screen, url=url, prefer_unread=True) if candidate.kind == "row"]
    paired = []
    for candidate in found:
        expanded = (candidate.box[0] - 6, candidate.box[1] - 6, candidate.box[2] + 6, candidate.box[3] + 6)
        hits = [control for control in rows if _intersects(expanded, control.box)]
        if len(hits) == 1:
            paired.append((candidate, hits[0]))
    if not paired:
        return _finish(spans, bag, outcome="wait", reason="no_target", screen=screen)
    paired.sort(key=lambda item: (item[1].box[1], item[1].box[0]))
    bounds = []
    for index, (candidate, control) in enumerate(paired, start=1):
        control.choice = f"c{index}"
        bounds.append(Bound(control.choice, candidate.key, "row", _ibox(control.box),
                            (int(control.center[0]), int(control.center[1])), candidate.description,
                            control.thread, control.name))
    target = select_target(TASK_ROW, bounds)
    choices = {item.choice: item.description for item in bounds}
    choices["hold"] = _HOLD
    choices["wait"] = _WAIT
    evidence = "\n".join(f"{item.choice}: {item.label}" for item in bounds if item.label)
    encoded = label(image, [(item.choice, item.box) for item in bounds])
    choice, confidence = _ask(session, vision, spans, bag, encoded, TASK_ROW, choices, evidence[:1500],
                              None, transport, boundary, raw, "full", url)
    return _agree_and_open(session, spans, bag, choice, confidence, bounds, target, rows,
                           timeout=timeout, pause=pause, screen=screen)


def _agree_and_open(session, spans, bag, choice, confidence, bounds, target, rows, *, timeout, pause, screen):
    if choice == "wait":
        return _finish(spans, bag, outcome="wait", reason="wait", screen=screen)
    if choice in ("hold",) or choice not in {item.choice for item in bounds} or confidence < MIN_CONFIDENCE:
        reason = "low_confidence" if choice not in ("hold", "wait") and confidence < MIN_CONFIDENCE else (
            choice if choice in ("hold", "wait") else "low_confidence")
        return _finish(spans, bag, outcome="hold", reason=reason, screen=screen)
    if target is None or choice != target.choice:
        bag["intercepted"] = target is not None
        return _finish(spans, bag, outcome="hold", reason="rejected_choice" if target is not None else "ambiguous",
                       screen=screen)
    matches = [control for control in rows if control.choice == choice]
    if len(matches) != 1:
        bag["intercepted"] = True
        return _finish(spans, bag, outcome="hold", reason="ambiguous", screen=screen)
    return _open_row(session, spans, bag, matches[0], timeout=timeout, pause=pause, screen=screen)


def _open_row(session, spans, bag, picked, *, timeout, pause, screen):
    with spans.span("revalidation"):
        fresh = _relocate(session, "#threads", thread=picked.thread)
    if fresh is None:
        bag["intercepted"] = True
        return _finish(spans, bag, outcome="hold", reason="stale_target", screen=screen)
    with spans.span("interaction"):
        failed = _click(session, fresh.ref)
    with spans.span("verification"):
        opened = _poll(session, lambda: _row_ready(session, picked.thread), timeout, pause)
    if failed and not opened:
        return _finish(spans, bag, outcome="hold", reason="click_failed", screen=screen, action=picked.choice)
    if not opened:
        landed = thread_id_of(session.page_url())
        wrong = bool(landed and landed != picked.thread)
        if wrong:
            bag["wrong_clicks"] += 1
        return _finish(spans, bag, outcome="wrong" if wrong else "hold",
                       reason="wrong_thread" if wrong else "timeout", screen=screen,
                       wrong_action=wrong, action=picked.choice)
    return _finish(spans, bag, outcome="acted", reason="row", action=picked.choice, screen="thread")


def _snapshot(session, selector):
    try:
        return session.snapshot(selector)
    except AgentError:
        return ""


def _row_ready(session, thread):
    if thread_id_of(session.page_url()) != thread:
        return None
    if "Close reel" in _snapshot(session, "#player"):
        return None
    pane = _snapshot(session, "#pane")
    if "Incoming message" in pane or "Outgoing message" in pane or len(pane.strip()) >= 12:
        return thread
    return None


def _reel_cards(session, snapshot):
    controls = _enrich(session, parse_controls(snapshot), want_href=False)
    cards = [control for control in controls if control.role == "button" and "close" not in control.name.lower()
             and control.box[3] - control.box[1] >= 80]
    cards.sort(key=lambda control: (control.box[1], control.box[0]))
    for index, control in enumerate(cards, start=1):
        control.choice = f"c{index}"
        control.side = side_of(snapshot, control.ref)
    return cards


def _reel(session, vision, spans, bag, *, procedure, timeout, pause, transport, boundary):
    url = session.page_url()
    thread = thread_id_of(url)
    if thread is None:
        return _finish(spans, bag, outcome="hold", reason="wrong_screen")
    if bound_mismatch(url):
        return _finish(spans, bag, outcome="hold", reason="wrong_screen")
    recovery = False
    while True:
        with spans.span("snapshot"):
            status, captured = _stable(session, "#pane", None)
        if status:
            return _finish(spans, bag, outcome="hold", reason=status)
        raw, image, snapshot, url = captured
        if thread_id_of(url) != thread:
            bag["wrong_clicks"] += 1
            return _finish(spans, bag, outcome="wrong", reason="wrong_thread", wrong_action=True)
        cards = _reel_cards(session, snapshot)
        if not cards:
            return _finish(spans, bag, outcome="wait", reason="no_target", screen=classify(image, url))
        choices = {card.choice: card.name or "Thread card." for card in cards}
        choices["hold"] = _HOLD
        choices["wait"] = _WAIT
        evidence = snapshot[:1500]
        encoded = label(image, [(card.choice, _ibox(card.box)) for card in cards])
        choice, confidence = _ask(session, vision, spans, bag, encoded, TASK_REEL, choices, evidence,
                                  None, transport, boundary, raw, "full", url)
        picked = _select_reel(choice, confidence, cards, snapshot, procedure, first=True)
        if picked is None and procedure == "phase1" and choice != "wait":
            picked, choice, confidence = _second_look(
                session, vision, spans, bag, cards, snapshot, thread, transport, boundary, pause)
        if choice == "wait" and picked is None:
            return _finish(spans, bag, outcome="wait", reason="wait")
        if picked is None:
            reason = "low_confidence" if confidence < MIN_CONFIDENCE and choice not in ("hold", "wait") else "hold"
            return _finish(spans, bag, outcome="hold", reason=reason)
        with spans.span("revalidation"):
            fresh = _relocate(session, "#pane", name=picked.name)
            if fresh is None or abs(fresh.center[1] - picked.center[1]) > 80 and fresh.name != picked.name:
                fresh = None
        if fresh is None:
            bag["intercepted"] = True
            return _finish(spans, bag, outcome="hold", reason="stale_target")
        with spans.span("interaction"):
            _click(session, fresh.ref)
        with spans.span("verification"):
            ready = _poll(session, lambda: _player_ready(session, thread, picked.name), timeout, pause)
            photo = False if ready else _photo_ready(session)
        if ready:
            return _finish(spans, bag, outcome="acted", reason="card", action=picked.choice, screen="overlay")
        if photo and procedure == "phase1" and not recovery:
            bag["wrong_clicks"] += 1
            if not _close_photo(session, spans, thread, timeout, pause):
                return _finish(spans, bag, outcome="wrong", reason="wrong_target", wrong_action=True,
                               action=picked.choice)
            bag["recovered"] = True
            recovery = True
            continue
        wrong = bool(photo or _photo_ready(session) or (thread_id_of(session.page_url()) not in (None, thread)))
        if wrong:
            bag["wrong_clicks"] += 1
        return _finish(spans, bag, outcome="wrong" if wrong else "hold",
                       reason="wrong_target" if wrong else "timeout", wrong_action=wrong, action=picked.choice)


def bound_mismatch(url):
    return False if thread_id_of(url) else True


def _reel_count(snapshot):
    return sum(1 for control in parse_controls(snapshot) if "reel" in control.name.lower()
               and "photo" not in control.name.lower())


def _select_reel(choice, confidence, cards, snapshot, procedure, first):
    del snapshot, first
    if choice in ("hold", "wait") or confidence < MIN_CONFIDENCE:
        return None
    picked = next((card for card in cards if card.choice == choice), None)
    if picked is None:
        return None
    if procedure == "baseline":
        bounds = [Bound(card.choice, card.choice, "card", _ibox(card.box),
                        (int(card.center[0]), int(card.center[1])), card.name, None, card.name)
                  for card in cards]
        target = select_target(TASK_REEL, bounds)
        if target is None or target.choice != choice:
            return None
    return picked


def _reel_count_from(cards):
    return sum(1 for card in cards if "reel" in card.name.lower() and "photo" not in card.name.lower())


def _second_look(session, vision, spans, bag, cards, snapshot, thread, transport, boundary, pause):
    del pause, thread
    reels = [card for card in cards if "reel" in card.name.lower() and "photo" not in card.name.lower()]
    subject = reels[0] if len(reels) == 1 else None
    if subject is None and len(cards) == 1:
        subject = cards[0]
    if subject is None:
        return None, "hold", 0.0
    bag["second_look"] = True
    with spans.span("crop"):
        raw = session.screenshot()
        image = open_png(raw)
        box = context_crop(subject.box, getattr(subject, "side", "in"))
    if box is None or image.size != (WIDTH, HEIGHT):
        return None, "hold", 0.0
    crop = png_bytes(image.crop(box))
    if not crop.startswith(b"\x89PNG\r\n\x1a\n") or not crop_covers(box, subject.box):
        return None, "hold", 0.0
    choices = {"yes": "This reel belongs to the latest incoming turn.",
               "no": "This reel does not belong to the latest incoming turn.", "hold": _HOLD}
    choice, confidence = _ask(
        session, vision, spans, bag, crop, REEL_LOOK, choices, snapshot[:1500], REEL_LOOK,
        transport, boundary, crop, "crop", session.page_url(), crop=box, card=_ibox(subject.box))
    if choice == "yes" and confidence >= MIN_CONFIDENCE:
        subject.choice = "yes"
        return subject, choice, confidence
    return None, choice, confidence


def _player_ready(session, thread, name):
    if thread_id_of(session.page_url()) != thread:
        return None
    player = _snapshot(session, "#player")
    if "Close reel" in player and name in player:
        return player
    return None


def _photo_ready(session):
    return "Close photo" in _snapshot(session, "#preview")


def _close_photo(session, spans, thread, timeout, pause):
    controls = _enrich(session, parse_controls(session.snapshot("#preview")), want_href=False)
    closes = [control for control in controls if "close photo" in control.name.lower()]
    if len(closes) != 1:
        return False
    with spans.span("interaction"):
        if _click(session, closes[0].ref):
            return False
    return _poll(session, lambda: _photo_closed(session, thread), timeout, pause) is not None


def _photo_closed(session, thread):
    if thread_id_of(session.page_url()) != thread or _photo_ready(session):
        return None
    return True


def _close(session, vision, spans, bag, *, timeout, pause, transport, boundary):
    url = session.page_url()
    thread = thread_id_of(url)
    if thread is None:
        return _finish(spans, bag, outcome="hold", reason="wrong_screen")
    with spans.span("snapshot"):
        status, captured = _stable(session, "#player", None)
    if status:
        return _finish(spans, bag, outcome="hold", reason=status)
    raw, image, snapshot, url = captured
    if thread_id_of(url) != thread:
        return _finish(spans, bag, outcome="hold", reason="wrong_thread")
    controls = _enrich(session, parse_controls(snapshot), want_href=False)
    closes = [control for control in controls if "close" in control.name.lower()]
    if len(closes) != 1:
        return _finish(spans, bag, outcome="wait", reason="no_target")
    closes[0].choice = "c1"
    choices = {"c1": closes[0].name, "hold": _HOLD, "wait": _WAIT}
    encoded = label(image, [("c1", _ibox(closes[0].box))])
    choice, confidence = _ask(session, vision, spans, bag, encoded, TASK_CLOSE, choices, snapshot[:1500],
                              None, transport, boundary, raw, "full", url)
    if choice != "c1" or confidence < MIN_CONFIDENCE:
        reason = "wait" if choice == "wait" else ("hold" if choice == "hold" else "low_confidence")
        return _finish(spans, bag, outcome="wait" if choice == "wait" else "hold", reason=reason)
    with spans.span("revalidation"):
        fresh = _relocate(session, "#player", name=closes[0].name)
    if fresh is None:
        bag["intercepted"] = True
        return _finish(spans, bag, outcome="hold", reason="stale_target")
    with spans.span("interaction"):
        _click(session, fresh.ref)
    with spans.span("verification"):
        ready = _poll(session, lambda: _closed(session, thread), timeout, pause)
    if not ready:
        return _finish(spans, bag, outcome="hold", reason="timeout", action="c1")
    return _finish(spans, bag, outcome="acted", reason="close", action="c1", screen="thread")


def _closed(session, thread):
    if thread_id_of(session.page_url()) != thread:
        return None
    if "Close reel" in _snapshot(session, "#player"):
        return None
    if len(_snapshot(session, "#pane").strip()) < 12:
        return None
    return True


def _demo():
    assert age_minutes("Asha 5h") == 300
    assert age_minutes("Bela 30m unread") == 30
    assert age_minutes("no clock") is None
    assert list_order('complementary "Newest at top"') == "newest"
    assert list_order("Conversations") is None

    class Row:
        def __init__(self, name, y):
            self.name, self.center = name, (10, y)

    top, low = Row("Asha 5h", 20), Row("Bela 1h", 90)
    assert choose_oldest([top, low], "newest") is top
    assert choose_oldest([Row("Asha 1h", 20), Row("Bela 4h", 90)], None) .name.startswith("Bela")
    assert choose_oldest([Row("Asha 2h", 20), Row("Bela 2h", 90)], None).center[1] == 90
    assert choose_oldest([Row("Asha", 20), Row("Bela", 90)], None) is None
    assert choose_oldest([Row("Asha", 20), Row("Bela", 90)], "newest").center[1] == 90
    assert choose_oldest([Row("Asha unread", 40)], None).name.startswith("Asha")
    assert parse_members("c1+c2", ["c1", "c2"]) == ["c1", "c2"]
    assert parse_members("c2+c1", ["c1", "c2"]) is None
    crop = context_crop((500, 200, 700, 380), "in")
    assert crop and crop_covers(crop, (500, 200, 700, 380))
    with open(__file__, encoding="utf-8") as handle:
        body = handle.read().split("def _demo", 1)[0]
    for hook in ("shadowLocate", "shadowToken", "shadowOutcome", "shadowProbe", "shadowMutate"):
        assert hook not in body


if __name__ == "__main__":
    _demo()

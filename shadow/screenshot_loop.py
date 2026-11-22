"""One screenshot turn: inbox, thread, optional reel, and one verified reply.

Gemini Decision 1 picks among the actions found on the current image. A click
happens only after the same control is still there. Sending is off unless the
caller passes a deliver function.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from shadow.chromium import BrowserError
from shadow.contracts import ReplyDraft
from shadow.decisions import parse_role
from shadow.gemini_client import ProviderFailure, generate
from shadow.screen_decision import DecisionError, choose_action
from shadow.targets import classify, offer, thread_box
from shadow.vision_prompts import REEL_SYSTEM, TRANSCRIPT_SYSTEM
from shadow.viewport import HEIGHT, WIDTH, digest, label, open_png, png_bytes, region_stable

log = logging.getLogger("shadow.screenshot")

MIN_CONFIDENCE = 0.80
HEADER = (360, 60, 980, 140)
PER_CHECK = 3
IST = ZoneInfo("Asia/Kolkata")
THREAD_ID = re.compile(r"/direct/t/([0-9]+)/?")
_REACTION = re.compile(r"\breacted\b|\bliked a message\b|\byou reacted\b", re.I)
_RECEIPTS = {"seen", "delivered", "sent"}
TASK_ROW = ("Select the oldest visibly unread DM among the offered rows. Look for the unread dot "
            "or clear unread styling on that row. A recent preview alone does not establish unread "
            "status. Use the visible order or age to choose among confirmed unread rows. "
            "Skip stories and reaction-only rows. If none qualifies, hold.")
TASK_NAMES = ("Compare the visible display names on the inbox DM rows. Choose duplicate if two "
              "rows show the same display name, unique if every name is readable and distinct, "
              "or hold if a name is hidden, truncated, or unclear.")
TASK_REEL = ("Select the visible reel card for the latest incoming turn. A card immediately above "
             "a separate incoming caption about the clip can be part of that same turn. Check "
             "left-side alignment and reel cues such as a shared reel label and play control. "
             "If two labels cover the same reel, choose the one enclosing the whole card. "
             "Hold for older or outgoing reels, a photo or link instead of a reel, or multiple plausible cards.")
TASK_CLOSE = ("Select the visible close control for the open media player so Shadow can return "
              "to the same conversation. Only the player close control qualifies. Hold if its "
              "position or purpose is unclear.")
NAMES = {"unique": "Every visible inbox row has a distinct display name.",
         "duplicate": "Two visible rows show the same display name.",
         "hold": "The names cannot be read, so do not choose a row."}


def _done(**fields):
    fields.setdefault("sends", 0)
    fields.setdefault("reactions", 0)
    log.info("screenshot_turn outcome=%s reason=%s", fields.get("outcome"), fields.get("reason"))
    return fields


def thread_id_of(url):
    match = THREAD_ID.fullmatch(urlsplit(url or "").path)
    return match.group(1) if match else None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def parse_transcript(raw):
    if type(raw) is str:
        raw = json.loads(raw, object_pairs_hook=_pairs)
    if type(raw) is not dict or set(raw) != {"bubbles", "incomplete"}:
        raise ValueError("transcript fields")
    bubbles = []
    if type(raw["bubbles"]) is not list or type(raw["incomplete"]) is not bool:
        raise ValueError("transcript types")
    for index, bubble in enumerate(raw["bubbles"], start=1):
        if type(bubble) is not dict or set(bubble) != {
            "order", "side", "text", "quote", "media", "unreadable", "clipped"
        }:
            raise ValueError("bubble fields")
        if bubble["order"] != index or bubble["side"] not in ("left", "right", "unknown"):
            raise ValueError("bubble order")
        if type(bubble["text"]) is not str or type(bubble["unreadable"]) is not bool or type(bubble["clipped"]) is not bool:
            raise ValueError("bubble text")
        if bubble["quote"] is not None and type(bubble["quote"]) is not str:
            raise ValueError("quote")
        if bubble["media"] is not None and type(bubble["media"]) is not str:
            raise ValueError("media")
        bubbles.append(dict(bubble))
    return {"bubbles": bubbles, "incomplete": raw["incomplete"]}


def sample_interval(events, interval, window=8.0, limit=8):
    """What a fixed interval would keep of a scripted reel. Used to compare 1s, 2s and 4s."""
    def at(moment):
        current = events[0][1]
        for stamp, token in events:
            if stamp <= moment:
                current = token
        return current

    samples, moment = [], 0.0
    while len(samples) < limit and moment <= window:
        token = at(moment)
        if not samples or token != samples[-1][1]:
            samples.append((moment, token))
        moment = round(moment + interval, 3)
    return samples


def fingerprint(bubbles):
    payload = [(bubble["order"], bubble["side"], bubble["text"], bubble["quote"], bubble["media"],
                bubble["unreadable"], bubble["clipped"]) for bubble in bubbles]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def latest_inbound(bubbles):
    inbound = [bubble for bubble in bubbles if bubble["side"] != "right"]
    return inbound[-1] if inbound else None


def needs_reel(bubbles):
    if not bubbles or bubbles[-1]["side"] != "left":
        return False
    if bubbles[-1].get("media"):
        return True
    return len(bubbles) >= 2 and bubbles[-2]["side"] == "left" and bool(bubbles[-2].get("media"))


def evidence_problem(bubbles, media):
    if bubbles and bubbles[-1]["side"] == "right":
        return "owner_answered"
    latest = latest_inbound(bubbles)
    if latest is None or latest["side"] != "left":
        return "uncertain_sender"
    if latest["unreadable"] or latest["clipped"]:
        return "unreadable"
    if _REACTION.search(latest["text"] or ""):
        return "reaction"
    if needs_reel(bubbles) and (not media or media.get("stalled") or not media.get("advanced", True)):
        return "media_incomplete"
    return None


def within_hours(now):
    """India daytime, 06:00 through 23:59. Midnight through 05:59 starts nothing."""
    if type(now) is not datetime or now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(IST).hour >= 6


def _norm(text):
    return " ".join((text or "").split())


def _visible(bubbles):
    kept = []
    for bubble in bubbles:
        text = _norm(bubble.get("text"))
        if bubble.get("side") == "right" and text.casefold() in _RECEIPTS:
            continue
        kept.append({**bubble, "text": text})
    return kept


def turn_change(before, after):
    """None when the inbound turn is the same. Receipts and whitespace do not count.

    A post-playback transcript can drop the reel label or a Seen line. A new
    inbound message, an owner reply, or an unreadable latest line still stops the draft.
    """
    if not after:
        return "unreadable"
    before_v, after_v = _visible(before), _visible(after)
    before_in = tuple(bubble["text"] for bubble in before_v if bubble["side"] == "left")
    after_in = tuple(bubble["text"] for bubble in after_v if bubble["side"] == "left")
    before_right = tuple(bubble["text"] for bubble in before_v if bubble["side"] == "right")
    after_right = tuple(bubble["text"] for bubble in after_v if bubble["side"] == "right")
    inbound = [bubble for bubble in after_v if bubble["side"] == "left"]
    if after_in == before_in and after_right == before_right:
        if inbound and (inbound[-1]["unreadable"] or inbound[-1]["clipped"]):
            return "unreadable"
        return None
    if inbound and (inbound[-1]["unreadable"] or inbound[-1]["clipped"]):
        return "unreadable"
    previous = [bubble for bubble in before_v if bubble["side"] == "left"]
    if inbound and _REACTION.search(inbound[-1]["text"] or "") and (
            not previous or previous[-1]["text"] != inbound[-1]["text"]):
        return "reaction"
    if after_right != before_right and after_right[:len(before_right)] == before_right and len(after_right) > len(before_right):
        return "owner_answered"
    if after_in != before_in:
        return "stale_turn"
    return "owner_answered"


def answered_decision(storage, conversation_id, bubbles):
    """A confirmed or unresolved send for this same inbound text is not sent again."""
    incoming = tuple(_norm(bubble["text"]) for bubble in bubbles if bubble["side"] == "left")
    if not incoming:
        return None
    blocking = {item["decision_id"] for item in storage.outbox_items(conversation_id)
                if item["state"] in ("confirmed", "uncertain", "attempting")}
    for row in storage.decisions_for(conversation_id):
        if row["id"] not in blocking:
            continue
        prior = json.loads(row["record_json"]).get("bubbles") or []
        if tuple(_norm(bubble.get("text")) for bubble in prior if bubble.get("side") == "left") == incoming:
            return row["id"]
    return None


REPLY_PROMPT_VERSION = "screenshot-draft-2"


def voice_text():
    from importlib.resources import files
    return files("shadow").joinpath("reply_style.md").read_text(encoding="utf-8").strip()


def reply_system(storage=None, conversation_id=None):
    extra = ()
    if storage is not None and conversation_id is not None:
        from shadow.context_memory import ContextMemory
        extra = ContextMemory(storage).rules(conversation_id)
    rules = ("Return one JSON object and nothing else, with exactly these fields: "
             '{"schema_version":1,"action":"reply","reason_code":"ok","messages":["one short dm"],'
             '"evidence_event_ids":[],"used_memory_ids":[]}. '
             "If essential evidence is missing, use action skip and messages []. "
             "When the contact asks for spoken words, a quote from audio, or other audio details "
             "that the supplied evidence does not establish, return action skip with messages []. "
             "Incoming messages in a group may be from different people. Respond to the latest "
             "incoming turn without guessing who wrote it or attributing earlier messages to that person. "
             "Keep the reason in reason_code. A skip never contains a chat message. "
             "action hold means the same as skip. Do not invent prices, times, names or speech.")
    return "\n\n".join((voice_text(), *extra, rules))


def coerce_reply(raw):
    value = json.loads(raw, object_pairs_hook=_pairs)
    if type(value) is dict and value.get("action") == "hold":
        value["action"] = "skip"
        value["messages"] = []
        value.setdefault("schema_version", 1)
        value.setdefault("reason_code", "hold")
        value.setdefault("evidence_event_ids", [])
        value.setdefault("used_memory_ids", [])
        return json.dumps(value)
    return raw


def ensure_schema(storage):
    storage.conn.execute(
        "CREATE TABLE IF NOT EXISTS screenshot_revision ("
        "conversation_id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, revision INTEGER NOT NULL)")
    storage.conn.execute(
        "CREATE TABLE IF NOT EXISTS screenshot_media ("
        "cache_key TEXT PRIMARY KEY, summary_json TEXT NOT NULL)")

    storage.conn.execute(
        "CREATE TABLE IF NOT EXISTS screenshot_inbox_retry ("
        "account_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, attempts INTEGER NOT NULL)")
    storage.conn.execute(
        "CREATE TABLE IF NOT EXISTS screenshot_queue ("
        "account_id TEXT NOT NULL, thread_id TEXT NOT NULL, attempts INTEGER NOT NULL, "
        "queued_at TEXT NOT NULL, PRIMARY KEY (account_id, thread_id))")


def claim_inbox_retry(storage, account_id):
    """An opened message can stop being unread before transcription succeeds."""
    ensure_schema(storage)
    with storage.transaction() as conn:
        row = conn.execute(
            "SELECT thread_id, attempts FROM screenshot_inbox_retry WHERE account_id = ?",
            (account_id,)).fetchone()
        if row is None:
            return None
        if row["attempts"] >= 3:
            conn.execute("DELETE FROM screenshot_inbox_retry WHERE account_id = ?", (account_id,))
            return None
        conn.execute("UPDATE screenshot_inbox_retry SET attempts = attempts + 1 WHERE account_id = ?",
                     (account_id,))
        return row["thread_id"]


def note_opened(storage, account_id, thread_id):
    """Remember a chat whose unread badge may disappear before the reply is ready."""
    ensure_schema(storage)
    storage.conn.execute(
        "INSERT INTO screenshot_queue (account_id, thread_id, attempts, queued_at) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(account_id, thread_id) DO NOTHING",
        (account_id, thread_id, utcnow().isoformat()))


def claim_next(storage, account_id, skip=()):
    ensure_schema(storage)
    skipped = tuple(skip)
    with storage.transaction() as conn:
        query = ("SELECT thread_id FROM screenshot_queue WHERE account_id = ? AND attempts < 3 "
                 + ("AND thread_id NOT IN (%s) " % ",".join("?" for _ in skipped) if skipped else "")
                 + "ORDER BY queued_at, thread_id LIMIT 1")
        row = conn.execute(query, (account_id, *skipped)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE screenshot_queue SET attempts = attempts + 1 WHERE account_id = ? AND thread_id = ?",
            (account_id, row["thread_id"]))
        return row["thread_id"]


def finish_queued(storage, account_id, thread_id, result):
    keep = result["outcome"] in ("pending", "wait", "stale") or result.get("recapture")
    if result.get("delivery") in ("uncertain", "outside_hours"):
        keep = True
    if not keep:
        storage.conn.execute(
            "DELETE FROM screenshot_queue WHERE account_id = ? AND thread_id = ?",
            (account_id, thread_id))


def finish_inbox_turn(storage, account_id, result):
    if result["outcome"] in ("login", "paused"):
        return
    retry = (result["outcome"] in ("pending", "wait", "stale") or result.get("recapture")
             or result.get("delivery") in ("uncertain", "outside_hours"))
    if retry:
        cursor = storage.conn.execute(
            "DELETE FROM screenshot_inbox_retry WHERE account_id = ? AND attempts >= 3",
            (account_id,))
        if cursor.rowcount:
            result["inbox_retry_exhausted"] = True
    else:
        storage.conn.execute("DELETE FROM screenshot_inbox_retry WHERE account_id = ?", (account_id,))


def remember(storage, conversation_id, fingerprint_value):
    ensure_schema(storage)
    with storage.transaction() as conn:
        row = conn.execute("SELECT fingerprint, revision FROM screenshot_revision WHERE conversation_id = ?",
                           (conversation_id,)).fetchone()
        if row and row["fingerprint"] == fingerprint_value:
            return row["revision"]
        revision = (row["revision"] + 1) if row else 1
        conn.execute(
            "INSERT INTO screenshot_revision (conversation_id, fingerprint, revision) VALUES (?, ?, ?) "
            "ON CONFLICT (conversation_id) DO UPDATE SET fingerprint = excluded.fingerprint, "
            "revision = excluded.revision", (conversation_id, fingerprint_value, revision))
        conn.execute(
            "UPDATE decisions SET status = 'superseded' WHERE conversation_id = ? "
            "AND source_revision != ? AND status = 'recorded'", (conversation_id, revision))
        return revision


def active_draft(storage, conversation_id, revision, fingerprint_value):
    for row in storage.decisions_for(conversation_id):
        if row["status"] != "recorded" or row["source_revision"] != revision:
            continue
        if json.loads(row["record_json"]).get("fingerprint") == fingerprint_value:
            return row
    return None


def _save(storage, conversation_id, revision, fingerprint_value, draft, bubbles, media, versions, state):
    record = {"fingerprint": fingerprint_value, "bubbles": bubbles, "media": media,
              "reply": {"action": draft.action, "reason_code": draft.reason_code,
                        "messages": list(draft.messages)},
              "viewport": f"{WIDTH}x{HEIGHT}@1"}
    decision_id = storage.add_decision(
        conversation_id=conversation_id, source_revision=revision,
        record_json=json.dumps(record, ensure_ascii=False), versions_json=json.dumps(versions),
        status="recorded")
    storage.set_work(conversation_id, inbound_revision=revision, state=state)
    storage.update_work(conversation_id, state=state,
                        held_reason=None if draft.action == "reply" else draft.reason_code)
    return decision_id


def _pending(storage, conversation_id, revision, reason):
    storage.set_work(conversation_id, inbound_revision=revision, state="pending")
    storage.update_work(conversation_id, state="pending", held_reason=reason)


def draft_and_save(storage, *, account_id, thread_id, bubbles, media, primary, fallback, now, reread=None):
    """Save one draft for this inbound revision, or leave the turn pending."""
    if type(now) is not datetime or now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    runtime = storage.get_runtime() or {}
    if runtime.get("pause_reason"):
        return _done(outcome="paused", reason=runtime["pause_reason"])
    if runtime.get("account_id") and runtime["account_id"] != account_id:
        return _done(outcome="hold", reason="wrong_account")
    parsed = parse_transcript({"bubbles": bubbles, "incomplete": False})
    bubbles = parsed["bubbles"]
    conversation_id = storage.get_or_create_conversation(account_id, str(thread_id))
    conversation = storage.get_conversation(conversation_id)
    if conversation.get("paused_reason"):
        return _done(outcome="paused", reason=conversation["paused_reason"])
    from shadow.storage import parse_iso
    until = parse_iso(conversation.get("owner_active_until"))
    if until is not None and until > now:
        return _done(outcome="hold", reason="owner_active")
    token = fingerprint(bubbles)
    revision = remember(storage, conversation_id, token)
    prior = answered_decision(storage, conversation_id, bubbles)
    if prior:
        return _done(outcome="existing", reason="already_replied", decision_id=prior,
                     conversation_id=conversation_id, revision=revision)
    existing = active_draft(storage, conversation_id, revision, token)
    if existing:
        return _done(outcome="existing", reason="same_revision", decision_id=existing["id"],
                     conversation_id=conversation_id, revision=revision)
    problem = evidence_problem(bubbles, media)
    system = reply_system(storage, conversation_id)
    user = json.dumps({"bubbles": bubbles, "media": media}, ensure_ascii=False)

    def changed():
        if reread is None:
            return None
        try:
            observed = reread()
            if observed is None:
                return "wrong_thread"
            fresh = parse_transcript({"bubbles": observed, "incomplete": False})["bubbles"]
        except ProviderFailure as exc:
            return exc.code
        except ValueError:
            return "unreadable"
        reason = turn_change(bubbles, fresh)
        if reason == "stale_turn":
            remember(storage, conversation_id, fingerprint(fresh))
            _pending(storage, conversation_id, revision + 1, "stale_turn")
        return reason

    if problem:
        stale = changed()
        if stale in ("rate_limited", "quota", "timeout", "provider", "auth"):
            _pending(storage, conversation_id, revision, stale)
            return _done(outcome="pending", reason=stale, conversation_id=conversation_id, revision=revision)
        if stale:
            return _done(outcome="stale" if stale == "stale_turn" else "hold", reason=stale,
                         conversation_id=conversation_id)
        draft = ReplyDraft("skip", problem, (), (), ())
        decision_id = _save(storage, conversation_id, revision, token, draft, bubbles, media,
                            {"model": "local", "prompt": REPLY_PROMPT_VERSION}, "held")
        return _done(outcome="hold", reason=problem, decision_id=decision_id,
                     conversation_id=conversation_id, revision=revision)

    meta = {"model": "unspecified"}
    used = "primary"
    try:
        raw, meta = primary(system, user)
    except ProviderFailure as exc:
        if fallback is None:
            _pending(storage, conversation_id, revision, exc.code)
            return _done(outcome="pending", reason=exc.code, conversation_id=conversation_id, revision=revision)
        try:
            raw, meta = fallback(system, user)
            used = "fallback"
        except ProviderFailure as second:
            _pending(storage, conversation_id, revision, second.code)
            return _done(outcome="pending", reason=second.code, conversation_id=conversation_id,
                         revision=revision)
    # A primary skip/hold returns here without calling fallback. Fallback runs only after ProviderFailure.
    try:
        outcome = parse_role("reply", coerce_reply(raw), set(), max_items=1, max_chars=180, max_total=180)
    except (ValueError, TypeError):
        outcome = None
    stale = changed()
    if stale in ("rate_limited", "quota", "timeout", "provider", "auth"):
        _pending(storage, conversation_id, revision, stale)
        return _done(outcome="pending", reason=stale, conversation_id=conversation_id,
                     revision=revision, used=used)
    if stale:
        return _done(outcome="stale" if stale == "stale_turn" else "hold", reason=stale,
                     conversation_id=conversation_id, used=used)
    if outcome is None or outcome.failure or outcome.value is None:
        draft = ReplyDraft("skip", "invalid_output", (), (), ())
        decision_id = _save(storage, conversation_id, revision, token, draft, bubbles, media,
                            {"model": meta.get("model"), "prompt": REPLY_PROMPT_VERSION}, "held")
        return _done(outcome="hold", reason="invalid_output", decision_id=decision_id,
                     conversation_id=conversation_id, revision=revision, used=used)
    again = active_draft(storage, conversation_id, revision, token)
    if again:
        return _done(outcome="existing", reason="same_revision", decision_id=again["id"],
                     conversation_id=conversation_id, revision=revision)
    draft = outcome.value
    state = "drafted" if draft.action == "reply" else "held"
    decision_id = _save(storage, conversation_id, revision, token, draft, bubbles, media,
                        {"model": meta.get("model"), "used": used, "latency_ms": meta.get("latency_ms"),
                         "usage": meta.get("usage"), "prompt": REPLY_PROMPT_VERSION}, state)
    return _done(outcome="draft" if draft.action == "reply" else "hold",
                 reason=draft.reason_code, decision_id=decision_id, conversation_id=conversation_id,
                 revision=revision, used=used, model=meta.get("model"))


def _choices(candidates):
    choices = {candidate.key: candidate.description for candidate in candidates}
    choices["hold"] = "Hold. The right control is missing, covered, ambiguous, or unsafe."
    choices["wait"] = "Wait for a fresh screenshot. The screen is still loading."
    return choices


def act(browser, vision, *, task, before_click=None, timeout=3.0, sleep=None, names=True, skip_keys=()):
    """One verified click, scroll, wait, or hold. Never clicks a stale box."""
    pause = time.sleep if sleep is None else sleep
    try:
        first = browser.screenshot()
        image = open_png(first)
    except (OSError, ValueError):
        return _done(outcome="hold", reason="bad_capture", recapture=True)
    if image.size != (WIDTH, HEIGHT):
        return _done(outcome="hold", reason="viewport", recapture=True)
    url = browser.page_url()
    screen = classify(image, url)
    if screen == "login":
        return _done(outcome="login", reason="login_required", screen=screen)
    if screen in ("blank", "header_only", "unknown"):
        return _done(outcome="wait", reason=screen, screen=screen, recapture=True)
    found = offer(image, screen, url=url)
    if task == TASK_ROW:
        found = [candidate for candidate in found if candidate.kind == "row" and candidate.key not in skip_keys]
    elif task == TASK_REEL:
        found = [candidate for candidate in found if candidate.kind == "card"]
    elif task == TASK_CLOSE:
        found = [candidate for candidate in found if candidate.kind == "close"]
    if not found:
        return _done(outcome="wait", reason="no_target", screen=screen, recapture=True)
    choices = _choices(found)
    if names and task == TASK_ROW and sum(candidate.kind == "row" for candidate in found) >= 2:
        try:
            verdict, confidence = vision.choose(first, TASK_NAMES, NAMES, found)
        except ProviderFailure as exc:
            return _done(outcome="pending" if exc.code in ("rate_limited", "quota", "timeout", "provider") else "hold",
                         reason=exc.code, recapture=True)
        if verdict != "unique" or confidence < MIN_CONFIDENCE:
            return _done(outcome="hold", reason="ambiguous_duplicate", screen=screen)
    try:
        choice, confidence = vision.choose(label(image, [(item.key, item.box) for item in found]),
                                           task, choices, found)
    except ProviderFailure as exc:
        fresh = browser.screenshot()
        return _done(outcome="pending" if exc.code in ("rate_limited", "quota", "timeout", "provider") else "hold",
                     reason=exc.code, recapture=True, capture=digest(fresh))
    except TimeoutError:
        return _done(outcome="hold", reason="timeout", recapture=True, capture=digest(browser.screenshot()))
    if choice in ("hold", "wait") or choice not in choices or confidence < MIN_CONFIDENCE:
        reason = choice if choice in ("hold", "wait") else "low_confidence"
        return _done(outcome="wait" if choice == "wait" else "hold", reason=reason, screen=screen)
    target = next(candidate for candidate in found if candidate.key == choice)
    if before_click:
        before_click()
    second = browser.screenshot()
    if browser.page_url() != url:
        return _done(outcome="hold", reason="stale_screenshot", recapture=True)
    try:
        fresh = open_png(second)
    except ValueError:
        return _done(outcome="hold", reason="bad_capture", recapture=True)
    if classify(fresh, browser.page_url()) != screen:
        return _done(outcome="hold", reason="unexpected_screen", recapture=True)
    again = offer(fresh, screen, url=url)
    match = next((candidate for candidate in again if candidate.key == target.key), None)
    moved = match is None or abs(match.center[0] - target.center[0]) > 4 or abs(match.center[1] - target.center[1]) > 4
    stable_box = HEADER if target.kind == "scroll" else target.box
    if moved or not region_stable(image, fresh, stable_box):
        return _done(outcome="hold", reason="target_moved", recapture=True)
    if target.kind == "scroll":
        browser.scroll(match.center[0], match.center[1], 400)
    else:
        browser.click(match.center[0], match.center[1])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        after = browser.screenshot()
        if after == second:
            pause(0.1)
            continue
        try:
            after_image = open_png(after)
        except ValueError:
            break
        after_screen = classify(after_image, browser.page_url())
        if target.kind == "scroll":
            if after_screen == "thread" and region_stable(image, after_image, HEADER):
                return _done(outcome="acted", reason="scroll", action="scroll", screen=after_screen)
        elif after_screen == target.expects:
            return _done(outcome="acted", reason=target.kind, action=target.key, screen=after_screen)
        pause(0.1)
    return _done(outcome="hold", reason="timeout", recapture=True, capture=digest(browser.screenshot()))


def sample_player(grab, *, sleep=None, clock=None, limit=8, window=8.0):
    """4–8 timestamped frames when playback advances. Silence is not speech."""
    pause = time.sleep if sleep is None else sleep
    now = time.monotonic if clock is None else clock
    start = now()
    first = grab()
    frames = [(0.0, first)]
    pause(0.35)
    second = grab()
    fast = second != first
    if fast:
        frames.append((round(now() - start, 3), second))
    interval = 0.45 if fast else 1.0
    last, repeats = second, 0 if fast else 1
    while len(frames) < limit and now() - start < window and repeats < 2:
        pause(interval)
        blob = grab()
        stamp = round(now() - start, 3)
        if blob == last:
            repeats += 1
        else:
            repeats = 0
            frames.append((stamp, blob))
            last = blob
    distinct = len({blob for _, blob in frames})
    advanced = any(blob != first for _, blob in frames)
    return {"frames": frames[:limit], "advanced": advanced, "stalled": not advanced,
            "distinct": distinct, "elapsed": round(now() - start, 3), "interval": interval, "audio": False}


def media_key(frames, caption):
    digest_ = hashlib.sha256()
    for stamp, blob in frames:
        digest_.update(str(stamp).encode())
        digest_.update(b"\0")
        digest_.update(blob)
    digest_.update(caption.encode())
    return digest_.hexdigest()


def _header(image):
    return hashlib.sha256(png_bytes(image.crop(HEADER))).hexdigest()


def _bubble(order, side, text, media=None):
    return {"order": order, "side": side, "text": text, "quote": None, "media": media,
            "unreadable": False, "clipped": False}


def _delivery_block(storage, account_id, thread_id, allow_threads):
    if allow_threads is not None and thread_id not in set(allow_threads):
        return _done(outcome="hold", reason="not_authorized", thread_id=thread_id)
    conversation_id = storage.get_or_create_conversation(account_id, thread_id)
    conversation = storage.get_conversation(conversation_id)
    reason = conversation.get("paused_reason")
    if reason in ("group", "group_uncertain"):
        # Clear the old worker's automatic group block while preserving owner pauses.
        storage.set_paused_reason(conversation_id, None)
    elif reason:
        return _done(outcome="paused", reason=reason, thread_id=thread_id,
                     conversation_id=conversation_id)
    return None


def _draft_text(storage, saved):
    rows = storage.decisions_for(saved["conversation_id"])
    record = next(row for row in rows if row["id"] == saved["decision_id"])
    messages = json.loads(record["record_json"]).get("reply", {}).get("messages") or []
    return messages[0] if messages else ""


def _initial_screen(browser, *, attempts, sleep):
    pause = time.sleep if sleep is None else sleep
    for attempt in range(attempts):
        if attempt:
            pause(0.4)
        try:
            url = browser.page_url() or ""
            if "/stories/" in url:
                return "story"
            screen = classify(open_png(browser.screenshot()), url)
        except (BrowserError, ValueError):
            screen = "bad_capture"
        if screen not in ("bad_capture", "blank", "header_only", "unknown"):
            return screen
    return screen


def run_turn(browser, vision, storage, *, account_id, now, primary, fallback, sleep=None,
             expected_thread_id=None, track_inbox=False, deliver=None, allow_threads=None,
             skip_keys=(), clock=None):
    clock = clock or (lambda: now)
    runtime = storage.get_runtime() or {}
    if runtime.get("pause_reason"):
        return _done(outcome="paused", reason=runtime["pause_reason"])
    if runtime.get("account_id") and runtime["account_id"] != account_id:
        return _done(outcome="hold", reason="wrong_account")
    if deliver is not None and not within_hours(clock()):
        return _done(outcome="outside_hours", reason="outside_hours")
    opened = None
    screen = _initial_screen(browser, attempts=16 if deliver is not None else 1, sleep=sleep)
    if screen == "bad_capture":
        return _done(outcome="hold", reason="bad_capture", recapture=True)
    if screen == "story":
        return _done(outcome="hold", reason="story")
    if screen == "login":
        storage.set_runtime(account_id=account_id, mode=runtime.get("mode") or "observe",
                            autonomy_scope=runtime.get("autonomy_scope") or "none",
                            pause_reason="login_required", config_digest=runtime.get("config_digest"))
        return _done(outcome="login", reason="login_required")
    if screen in ("blank", "header_only", "unknown"):
        return _done(outcome="wait", reason=screen, recapture=True)
    if deliver is not None and not within_hours(clock()):
        return _done(outcome="outside_hours", reason="outside_hours")
    if expected_thread_id and thread_id_of(browser.page_url()) != expected_thread_id:
        return _done(outcome="hold", reason="wrong_thread", recapture=True)
    if screen == "inbox":
        # Scheduled delivery follows an unread row and binds to the thread URL after the click.
        opened = act(browser, vision, task=TASK_ROW, sleep=sleep, names=deliver is None, skip_keys=skip_keys)
        thread = thread_id_of(browser.page_url())
        if track_inbox and thread:
            ensure_schema(storage)
            storage.conn.execute(
                "INSERT OR REPLACE INTO screenshot_inbox_retry (account_id, thread_id, attempts) VALUES (?, ?, 1)",
                (account_id, thread))
            note_opened(storage, account_id, thread)
            conversation_id = storage.get_or_create_conversation(account_id, thread)
            if storage.get_work(conversation_id) is None:
                storage.set_work(conversation_id, inbound_revision=0, state="pending")
                storage.update_work(conversation_id, held_reason="opened")
        if opened["outcome"] != "acted":
            return opened
        screen = "thread"
    if classify(open_png(browser.screenshot()), browser.page_url()) == "overlay":
        closed = act(browser, vision, task=TASK_CLOSE, sleep=sleep)
        if closed["outcome"] != "acted":
            return closed
    if classify(open_png(browser.screenshot()), browser.page_url()) != "thread":
        return _done(outcome="wait", reason="header_only", recapture=True)
    thread_id = thread_id_of(browser.page_url())
    if not thread_id:
        return _done(outcome="hold", reason="unbound_thread")
    if deliver is not None:
        blocked = _delivery_block(storage, account_id, thread_id, allow_threads)
        if blocked:
            blocked["opened"] = opened["action"] if opened else None
            return blocked
    image = open_png(browser.screenshot())
    header = _header(image)

    def thread_crop(frame):
        current = browser.page_url()
        if thread_id_of(current) != thread_id or _header(frame) != header or classify(frame, current) != "thread":
            return None
        return png_bytes(frame.crop(thread_box(frame)))

    crop = thread_crop(image)
    if crop is None:
        return _done(outcome="hold", reason="wrong_thread", recapture=True, thread_id=thread_id)
    try:
        transcript = parse_transcript(vision.transcribe(crop))
    except ProviderFailure as exc:
        return _done(outcome="pending", reason=exc.code, recapture=True, thread_id=thread_id)
    except (ValueError, TypeError):
        return _done(outcome="hold", reason="unreadable", recapture=True, thread_id=thread_id)
    if transcript["incomplete"]:
        return _done(outcome="wait", reason="incomplete_transcript", recapture=True, thread_id=thread_id)
    if thread_crop(open_png(browser.screenshot())) != crop:
        return _done(outcome="wait", reason="transcript_capture_changed", recapture=True, thread_id=thread_id)
    bubbles = transcript["bubbles"]
    media = None

    def reread():
        fresh = thread_crop(open_png(browser.screenshot()))
        if fresh is None:
            return None
        if fresh == crop:
            return bubbles
        for _attempt in range(2):
            try:
                observed = parse_transcript(vision.transcribe(fresh))
            except ProviderFailure:
                raise
            except (ValueError, TypeError):
                observed = None
            if observed and not observed["incomplete"] and thread_crop(open_png(browser.screenshot())) == fresh:
                return observed["bubbles"]
            fresh = thread_crop(open_png(browser.screenshot()))
            if fresh is None:
                return None
        raise ValueError("unverified transcript")

    if not evidence_problem(bubbles, {"advanced": True}) and needs_reel(bubbles):
        started = act(browser, vision, task=TASK_REEL, sleep=sleep)
        if started["outcome"] != "acted":
            started["thread_id"] = thread_id
            return started
        sampled = sample_player(browser.screenshot, sleep=sleep)
        summary = None
        if sampled["advanced"]:
            try:
                summary = _summarize(storage, vision, sampled, _caption(bubbles))
            except ProviderFailure as exc:
                act(browser, vision, task=TASK_CLOSE, sleep=sleep)
                return _done(outcome="pending", reason=exc.code, recapture=True, thread_id=thread_id)
        closed = act(browser, vision, task=TASK_CLOSE, sleep=sleep)
        if closed["outcome"] != "acted" or _header(open_png(browser.screenshot())) != header:
            return _done(outcome="hold", reason="wrong_thread", recapture=True, thread_id=thread_id)
        if thread_id_of(browser.page_url()) != thread_id:
            return _done(outcome="hold", reason="wrong_thread", thread_id=thread_id)
        media = summary or {"advanced": False, "stalled": True, "audio": False, "speech": None,
                            "distinct": sampled["distinct"], "interval": sampled["interval"]}
    if not runtime.get("account_id"):
        storage.set_runtime(account_id=account_id, mode="observe", autonomy_scope="none")

    saved = draft_and_save(storage, account_id=account_id, thread_id=thread_id, bubbles=bubbles,
                           media=media, primary=primary, fallback=fallback, now=now, reread=reread)
    saved["opened"] = opened["action"] if opened else None
    saved["thread_id"] = thread_id
    if deliver is None or saved["outcome"] not in ("draft", "existing"):
        return saved
    if not within_hours(clock()):
        saved["delivery"] = "outside_hours"
        saved["sends"] = 0
        return saved
    text = _draft_text(storage, saved)
    if not text:
        saved["delivery"] = "no_reply"
        saved["sends"] = 0
        return saved
    try:
        observed = reread()
    except ProviderFailure as exc:
        saved["outcome"] = "pending"
        saved["reason"] = exc.code
        saved["delivery"] = "not_sent"
        saved["sends"] = 0
        return saved
    change = "wrong_thread" if observed is None else turn_change(bubbles, observed)
    if change:
        saved["delivery"] = "not_sent"
        saved["reason"] = change
        saved["sends"] = 0
        if change == "stale_turn":
            saved["outcome"] = "stale"
        return saved
    saved.update(deliver(
        browser, storage, thread=thread_id, decision=saved["decision_id"],
        conversation=saved["conversation_id"], revision=saved["revision"], text=text,
        authority="autonomous", allow=lambda: within_hours(clock())))
    return saved


def run_check(browser, vision, storage, *, account_id, now, primary, fallback, deliver,
              allow_threads=None, sleep=None, clock=None):
    """One scheduled inbox check, carrying forward unfinished chats."""
    clock = clock or (lambda: now)
    if not within_hours(clock()):
        return _done(outcome="outside_hours", reason="outside_hours", sends=0, results=[])
    recovered = storage.recover_after_restart()
    from shadow.chromium_send import reconcile
    results, sends, handled, rows = [], 0, set(), set()
    for item in storage.outbox_items():
        if item["state"] != "uncertain" or not within_hours(clock()):
            continue
        conversation = storage.get_conversation(item["conversation_id"])
        if conversation is None:
            continue
        if allow_threads is not None and conversation["thread_id"] not in set(allow_threads):
            continue
        status = reconcile(browser, storage, item, conversation["thread_id"])
        results.append({"outcome": "reconcile", "delivery": status, "sends": 0,
                        "thread_id": conversation["thread_id"]})
        handled.add(conversation["thread_id"])
    for _ in range(PER_CHECK):
        if not within_hours(clock()):
            break
        queued = claim_next(storage, account_id, skip=handled)
        if queued and allow_threads is not None and queued not in set(allow_threads):
            finish_queued(storage, account_id, queued, {"outcome": "hold"})
            continue
        if queued:
            browser.navigate(f"https://www.instagram.com/direct/t/{queued}/")
            result = run_turn(browser, vision, storage, account_id=account_id, now=clock(),
                              primary=primary, fallback=fallback, sleep=sleep,
                              expected_thread_id=queued, deliver=deliver, allow_threads=allow_threads,
                              clock=clock)
        else:
            browser.navigate("https://www.instagram.com/direct/inbox/")
            result = run_turn(browser, vision, storage, account_id=account_id, now=clock(),
                              primary=primary, fallback=fallback, sleep=sleep, track_inbox=True,
                              deliver=deliver, allow_threads=allow_threads, skip_keys=rows,
                              clock=clock)
        results.append(result)
        sends += result.get("sends") or 0
        if str(result.get("opened") or "").startswith("row-"):
            rows.add(result["opened"])
        thread = result.get("thread_id")
        if thread:
            handled.add(thread)
            finish_queued(storage, account_id, thread, result)
            finish_inbox_turn(storage, account_id, result)
        if result["outcome"] in ("login", "outside_hours"):
            break
        if result["outcome"] == "paused" and not result.get("thread_id"):
            break
        if not queued and result["outcome"] in ("hold", "wait") and result.get("reason") in (
                "hold", "no_target", "low_confidence"):
            break
    return _done(outcome="check", reason="done", sends=sends, results=results, recovered=recovered)


def _caption(bubbles):
    inbound = [bubble["text"] for bubble in bubbles if bubble["side"] == "left"]
    return inbound[-1] if inbound else ""


def _summarize(storage, vision, sampled, caption):
    ensure_schema(storage)
    key = media_key(sampled["frames"], caption)
    cached = storage.conn.execute("SELECT summary_json FROM screenshot_media WHERE cache_key = ?", (key,)).fetchone()
    if cached:
        summary = json.loads(cached[0])
        summary["cached"] = True
        return summary
    summary = vision.describe(sampled["frames"], caption, audio=False)
    summary["advanced"] = True
    summary["stalled"] = False
    summary["audio"] = False
    summary["speech"] = None
    summary["distinct"] = sampled["distinct"]
    summary["interval"] = sampled["interval"]
    summary["elapsed"] = sampled["elapsed"]
    storage.conn.execute("INSERT INTO screenshot_media (cache_key, summary_json) VALUES (?, ?)",
                         (key, json.dumps(summary)))
    return summary


def openai_primary(api_key):
    from shadow.model_openai import OpenAITransport, ProviderError

    def primary(system, user):
        started = time.monotonic()
        transport = OpenAITransport(
            base_url="https://api.openai.com/v1", api_key=api_key,
            models={"risk": "gpt-4o-mini", "reply": "gpt-4o-mini", "extraction": "gpt-4o-mini"},
            max_tokens=200, json_object=True)
        try:
            result = asyncio.run(transport.generate("reply", system, user))
        except ProviderError as exc:
            code = {"provider_rate_limited": "rate_limited", "provider_auth_failed": "auth"}.get(exc.code, "provider")
            raise ProviderFailure(code) from None
        except TimeoutError:
            raise ProviderFailure("timeout") from None
        return result.text, {"model": result.model, "usage": dict(result.usage),
                             "latency_ms": round((time.monotonic() - started) * 1000)}
    return primary


def gemini_fallback(keys):
    def fallback(system, user):
        started = time.monotonic()
        result = generate(model="gemini-3.5-flash-lite", thinking="MEDIUM", keys=keys,
                          system=system, parts=[{"text": user}])
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        return result["text"], result
    return fallback


class LiveVision:
    """Decision 1 routes. Flash-Lite reads. Neither receives a separate OCR transcript."""

    def __init__(self, keys):
        self.keys = keys

    def choose(self, png, task, choices, candidates):
        del candidates
        last = None
        started = time.monotonic()
        for key in self.keys:
            try:
                choice, confidence = choose_action(key, png, task, choices)
                log.info("decision_1 latency_ms=%s", round((time.monotonic() - started) * 1000))
                return choice, confidence
            except DecisionError as exc:
                last = exc
                text = str(exc)
                if "HTTP 429" in text:
                    raise ProviderFailure("rate_limited") from None
                if "HTTP 401" in text or "HTTP 403" in text:
                    continue
                raise ProviderFailure("provider") from None
        raise ProviderFailure("auth") from last

    def transcribe(self, png):
        import base64
        result = generate(model="gemini-3.5-flash-lite", keys=self.keys, system=TRANSCRIPT_SYSTEM, parts=[
            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(png).decode("ascii")}}])
        return json.loads(result["text"])

    def describe(self, frames, caption, audio=False):
        import base64
        parts = [{"text": json.dumps({"audio_available": audio, "nearby_chat": caption}, ensure_ascii=False)}]
        for stamp, blob in frames:
            parts.append({"text": f"t={stamp}"})
            parts.append({"inline_data": {"mime_type": "image/png",
                                          "data": base64.b64encode(blob).decode("ascii")}})
        result = generate(model="gemini-3.1-flash-lite", keys=self.keys, system=REEL_SYSTEM, parts=parts)
        data = _one_reel(json.loads(result["text"]), audio)
        data["model"] = result["model"]
        data["usage"] = result["usage"]
        return data


def _one_reel(data, audio):
    """Fold a per-frame list into the one summary the draft expects."""
    if type(data) is list:
        if not data or any(type(item) is not dict for item in data):
            raise ProviderFailure("provider")
        data = {
            "subject": str(data[-1].get("subject") or ""),
            "action": "; ".join(str(item.get("action") or "") for item in data),
            "on_screen_text": " | ".join(str(item.get("on_screen_text") or "") for item in data),
            "speech": None if not audio else data[-1].get("speech"),
            "missing": "; ".join(str(item.get("missing") or "") for item in data
                                 if item.get("missing") not in (None, "", "none")),
        }
    if type(data) is not dict or any(name not in data for name in ("subject", "action", "on_screen_text")):
        raise ProviderFailure("provider")
    if not audio:
        data["speech"] = None
    data.setdefault("missing", "")
    return data


def utcnow():
    return datetime.now(timezone.utc)

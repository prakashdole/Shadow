"""Opt-in navigation cycle. Screenshot stays the default; Playwright and agent-browser add a scoped snapshot."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from threading import Lock
from time import monotonic, sleep

from shadow.chromium import BrowserError
from shadow.gemini_client import ProviderFailure
from shadow.screenshot_loop import MIN_CONFIDENCE, TASK_CLOSE, TASK_REEL, TASK_ROW, _done
from shadow.targets import classify, offer
from shadow.viewport import HEIGHT, WIDTH, label, open_png


AGENT_BROWSER_VERSION = "0.45.0"
ARMS = ("screenshot", "playwright", "agent-browser")
_DECISION = Lock()
_BUTTON = re.compile(r'(button|link) "([^"]{0,120})"')
_BOX = re.compile(r"\[box=(\d+(?:\.\d+)?),(\d+(?:\.\d+)?),(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)\]")
_BIN = None


class ObserveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Bound:
    choice: str
    key: str
    kind: str
    box: tuple
    center: tuple
    description: str
    shadow: str | None
    label: str


class Spans:
    def __init__(self):
        self.t0 = monotonic()
        self.rows = []
        self.gemini = []

    @contextmanager
    def span(self, name):
        start = monotonic()
        try:
            yield
        finally:
            self.rows.append((name, start, monotonic()))

    @contextmanager
    def model(self):
        start = monotonic()
        try:
            yield
        finally:
            self.gemini.append((start, monotonic()))

    def report(self):
        e2e = monotonic() - self.t0
        gemini = union(self.gemini)
        return {"e2e_s": e2e, "gemini_s": gemini, "outside_gemini_s": max(0.0, e2e - gemini),
                "spans": [{"name": name, "start": start - self.t0, "end": end - self.t0}
                          for name, start, end in self.rows]}


def union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(end - start for start, end in merged)


def parse_controls(snapshot):
    found = []
    for line in (snapshot or "").splitlines():
        match = _BUTTON.search(line)
        if not match:
            continue
        box = None
        boxed = _BOX.search(line)
        if boxed:
            x, y, width, height = (float(value) for value in boxed.groups())
            box = (x, y, x + width, y + height)
        found.append({"name": match.group(2), "box": box})
    return found


def _inside(center, node):
    x, y = center
    return node["x"] <= x <= node["x"] + node["w"] and node["y"] <= y <= node["y"] + node["h"]


def _intersects(first, second):
    return not (first[2] <= second[0] or second[2] <= first[0] or first[3] <= second[1] or second[3] <= first[1])


def align_labels(candidates, controls, nodes):
    labels = {candidate.key: "" for candidate in candidates}
    if controls and all(control["box"] for control in controls):
        for control in controls:
            hits = [candidate for candidate in candidates if _intersects(candidate.box, control["box"])]
            if len(hits) == 1:
                labels[hits[0].key] = control["name"]
        return labels
    anchored = [node for node in nodes if any(_inside(candidate.center, node) for candidate in candidates)]
    if controls and len(anchored) == len(controls):
        claimed = set()
        for node, control in zip(anchored, controls):
            hits = [candidate for candidate in candidates
                    if candidate.key not in claimed and _inside(candidate.center, node)]
            if len(hits) == 1:
                labels[hits[0].key] = control["name"]
                claimed.add(hits[0].key)
    return labels


def select_target(task, bounds):
    """The one control this task can prove. None means the click would be a guess."""
    if task == TASK_ROW:
        marked = [item for item in bounds
                  if "unread dot" in item.description or "unread" in item.label.lower()]
        if not marked:
            return bounds[0] if len(bounds) == 1 else None
        return max(marked, key=lambda item: item.center[1])
    if task == TASK_REEL:
        named = [item for item in bounds if "reel" in item.label.lower() and "photo" not in item.label.lower()]
        if len(named) == 1:
            return named[0]
        media = [item for item in bounds if item.key.startswith("media")]
        return media[0] if len(media) == 1 else None
    if task == TASK_CLOSE:
        closes = [item for item in bounds if item.kind == "close"]
        if len(closes) == 1:
            return closes[0]
        named = [item for item in closes if "close" in item.label.lower()]
        return named[0] if len(named) == 1 else None
    return None


def input_tokens(usage):
    if not isinstance(usage, dict):
        return None
    for key in ("input_tokens", "total_input_tokens", "promptTokenCount",
                "prompt_token_count", "prompt_tokens"):
        value = usage.get(key)
        if type(value) is int:
            return value
    return None


def _scope(screen, url):
    if screen == "overlay":
        return "#player"
    if "/direct/t/" in (url or ""):
        return "#pane"
    return "#threads"


def _agent_bin():
    global _BIN
    override = os.environ.get("SHADOW_AGENT_BROWSER")
    if override:
        path = Path(override)
    else:
        if _BIN:
            return _BIN
        path = Path.home() / ".cache/shadow/agent-browser-0.45.0/node_modules/.bin/agent-browser"
    if not path.is_file():
        raise ObserveError("agent_browser_unavailable")
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise ObserveError("agent_browser_unavailable") from None
    if AGENT_BROWSER_VERSION not in (proc.stdout + proc.stderr):
        raise ObserveError("agent_browser_version")
    if not override:
        _BIN = str(path)
    return str(path)


def _agent(port, args):
    try:
        proc = subprocess.run(
            [_agent_bin(), "--session", f"shadow-pilot-{port}", "--cdp", str(port), *args],
            capture_output=True, text=True, timeout=12)
    except subprocess.TimeoutExpired:
        raise ObserveError("agent_browser_timeout") from None
    except ObserveError:
        raise
    except (OSError, ValueError):
        raise ObserveError("agent_browser_unavailable") from None
    if proc.returncode != 0 or not proc.stdout:
        raise ObserveError("agent_browser_failed")
    if "--json" in args:
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise ObserveError("agent_browser_failed") from None
    return proc.stdout


_SELECTED = {}


def agent_snapshot(port, path, selector):
    listed = _agent(port, ["tab", "--json"])
    tabs = [tab for tab in (listed.get("data") or {}).get("tabs") or [] if path in (tab.get("url") or "")]
    if len(tabs) != 1 or not tabs[0].get("targetId"):
        raise ObserveError("agent_browser_tab")
    target = tabs[0]["targetId"]
    if _SELECTED.get(port) != target:
        _agent(port, ["tab", target])
        _SELECTED[port] = target
    body = _agent(port, ["snapshot", "-s", selector, "-c", "-d", "6", "--json"])
    data = body.get("data") or {}
    if path not in (data.get("origin") or ""):
        _SELECTED.pop(port, None)
        raise ObserveError("agent_browser_tab")
    return data.get("snapshot") or ""


class _Playwright:
    port = None
    manager = None
    browser = None


_PW = _Playwright()


def playwright_snapshot(port, path, selector):
    from playwright.sync_api import sync_playwright

    try:
        if _PW.port != port or _PW.browser is None:
            if _PW.manager is not None:
                _PW.manager.stop()
            _PW.manager = sync_playwright().start()
            _PW.browser = _PW.manager.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            _PW.port = port
        pages = [page for context in _PW.browser.contexts for page in context.pages]
        match = []
        for page in pages:
            try:
                current = page.evaluate("location.pathname")
            except Exception:
                current = page.url
            if path in current or path in page.url:
                match.append(page)
        if len(match) != 1:
            raise ObserveError("playwright_tab")
        page = match[0]
        page.bring_to_front()
        if path not in page.evaluate("location.pathname"):
            raise ObserveError("playwright_tab")
        text = page.locator(selector).aria_snapshot(boxes=True, timeout=3000)
        if path not in page.evaluate("location.pathname"):
            raise ObserveError("playwright_changed")
        return text
    except ObserveError:
        raise
    except Exception:
        _PW.browser = None
        raise ObserveError("playwright_failed") from None


def snapshot(browser, arm, path, selector):
    port = getattr(browser, "devtools_port", None)
    if not port:
        raise ObserveError("agent_browser_unavailable" if arm == "agent-browser" else "playwright_failed")
    if arm == "agent-browser":
        return agent_snapshot(port, path, selector)
    if arm == "playwright":
        try:
            return playwright_snapshot(port, path, selector)
        except ObserveError as exc:
            if str(exc) != "playwright_tab":
                raise
            _PW.browser = None
            return playwright_snapshot(port, path, selector)
    raise ObserveError("playwright_failed")


def _metrics(browser):
    if hasattr(browser, "command"):
        browser.command("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT,
                        deviceScaleFactor=1, mobile=False)


def bind_tab(browser, path):
    if not hasattr(browser, "browser_command"):
        url = browser.page_url()
        if path and path not in url:
            raise ObserveError("wrong_tab")
        return url
    try:
        infos = browser.browser_command("Target.getTargets")["targetInfos"]
        pages = [item for item in infos if item.get("type") == "page"]
        if path:
            match = [item for item in pages if path in (item.get("url") or "")]
            if len(match) != 1:
                raise ObserveError("wrong_tab")
            target = match[0]["targetId"]
        else:
            target = getattr(browser, "target_id", None)
            if not any(item.get("targetId") == target for item in pages):
                raise ObserveError("browser_detach")
        saved = browser.session_id
        browser.session_id = None
        try:
            attached = browser.command("Target.attachToTarget", targetId=target, flatten=True)
            browser.session_id = attached["sessionId"]
            browser.target_id = target
            url = browser.page_url()
        except (BrowserError, OSError, KeyError, TypeError, AttributeError):
            browser.session_id = saved
            raise ObserveError("browser_detach") from None
    except ObserveError:
        raise
    except (BrowserError, OSError, KeyError, TypeError, AttributeError):
        raise ObserveError("browser_detach") from None
    if path and path not in url:
        raise ObserveError("wrong_tab")
    return url


def _eval(browser, expression):
    try:
        return browser.evaluate(expression)
    except (BrowserError, OSError, AttributeError, TypeError):
        raise ObserveError("bad_capture") from None


def _loads(browser, expression):
    value = _eval(browser, expression)
    if not isinstance(value, str):
        raise ObserveError("bad_capture")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise ObserveError("bad_capture") from None


def _shadow_at(nodes, center):
    hits = [node["id"] for node in nodes if _inside(center, node)]
    if len(hits) != 1:
        return None
    return hits[0]


def navigate(browser, vision, *, task, arm="screenshot", prefer_unread=False, timeout=3.0, sleep=None,
             transport=None, boundary=None, bound_thread=None, bound_path=None, bound_account=None,
             skip_keys=()):
    del prefer_unread
    if arm not in ARMS:
        raise ObserveError("bad_arm")
    with _DECISION:
        return _navigate(browser, vision, task=task, arm=arm, timeout=timeout, pause=sleep,
                         transport=transport, boundary=boundary, bound_thread=bound_thread,
                         bound_path=bound_path, bound_account=bound_account, skip_keys=set(skip_keys))


def _finish(spans, **fields):
    fields.update(spans.report())
    return _done(**fields)


def _sig(token):
    return token.get("href"), token.get("gen"), tuple(node.get("id") for node in token.get("nodes") or [])


def _capture(browser):
    raw = browser.screenshot()
    token = _loads(browser, "window.shadowToken()")
    if _sig(_loads(browser, "window.shadowToken()")) != _sig(token):
        return None
    return raw, token


def _navigate(browser, vision, *, task, arm, timeout, pause, transport, boundary, bound_thread,
              bound_path, bound_account, skip_keys):
    spans = Spans()
    base = {"evidence_source": arm, "evidence_useful": False, "fallback_reason": None, "sent": False,
            "calls": 0, "payload_bytes": 0, "input_tokens": None, "wrong_action": False, "stale_rejected": 0}
    try:
        with spans.span("readiness"):
            url = bind_tab(browser, bound_path)
            _metrics(browser)
            if bound_account is not None:
                account = _eval(browser, "document.documentElement.dataset.account || ''")
                if account != bound_account:
                    return _finish(spans, outcome="hold", reason="wrong_account", **base)
            from shadow.screenshot_loop import thread_id_of
            if bound_thread and thread_id_of(url) not in (None, bound_thread):
                return _finish(spans, outcome="hold", reason="wrong_thread", **base)
    except (ObserveError, BrowserError) as exc:
        reason = str(exc) if str(exc) in ("wrong_tab", "browser_detach", "bad_capture") else "browser_detach"
        return _finish(spans, outcome="hold", reason=reason, **base)

    captured = None
    for _attempt in range(2):
        with spans.span("screenshot"):
            try:
                captured = _capture(browser)
            except (ObserveError, OSError, ValueError):
                captured = None
        if captured:
            break
        base["stale_rejected"] += 1
    if not captured:
        return _finish(spans, outcome="hold", reason="stale_observation", **base)
    raw, token = captured
    try:
        image = open_png(raw)
    except (OSError, ValueError):
        return _finish(spans, outcome="hold", reason="bad_capture", **base)
    if image.size != (WIDTH, HEIGHT):
        return _finish(spans, outcome="hold", reason="viewport", **base)
    screen = classify(image, url)
    if screen == "login":
        return _finish(spans, outcome="login", reason="login_required", screen=screen, **base)
    if screen in ("blank", "header_only", "unknown"):
        return _finish(spans, outcome="wait", reason=screen, screen=screen, **base)
    found = [candidate for candidate in offer(image, screen, url=url, prefer_unread=True)
             if candidate.key not in skip_keys]
    if task == TASK_ROW:
        found = [candidate for candidate in found if candidate.kind == "row"]
    elif task == TASK_REEL:
        found = [candidate for candidate in found if candidate.kind == "card"]
    elif task == TASK_CLOSE:
        found = [candidate for candidate in found if candidate.kind == "close"]
    if not found:
        return _finish(spans, outcome="wait", reason="no_target", screen=screen, **base)

    source, fallback, text = arm, None, ""
    if arm != "screenshot":
        with spans.span("snapshot"):
            try:
                text = snapshot(browser, arm, bound_path or token["href"], _scope(screen, url))
            except ObserveError as exc:
                source, fallback, text = "screenshot_fallback", str(exc), ""
        _metrics(browser)
        try:
            if _sig(_loads(browser, "window.shadowToken()")) != _sig(token):
                base["stale_rejected"] += 1
                return _finish(spans, outcome="hold", reason="stale_observation", **base)
        except ObserveError:
            return _finish(spans, outcome="hold", reason="bad_capture", **base)
    ordered = sorted(found, key=lambda candidate: (candidate.center[1], candidate.center[0]))
    labels = align_labels(ordered, parse_controls(text), token.get("nodes") or [])
    bounds = []
    for index, candidate in enumerate(ordered, start=1):
        bounds.append(Bound(f"c{index}", candidate.key, candidate.kind, candidate.box, candidate.center,
                            candidate.description, _shadow_at(token.get("nodes") or [], candidate.center),
                            labels.get(candidate.key) or ""))
    evidence = "\n".join(f"{item.choice}: {item.label}" for item in bounds if item.label)[:1500]
    useful = bool(evidence) and source == arm and arm != "screenshot"
    if source != arm:
        evidence = ""
    base.update(evidence_source=source, evidence_useful=useful, fallback_reason=fallback)
    target = select_target(task, bounds)
    choices = {item.choice: item.description for item in bounds}
    choices["hold"] = "Hold. The right control is missing, covered, ambiguous, or unsafe."
    choices["wait"] = "Wait for a fresh screenshot. The screen is still loading."
    with spans.span("encode"):
        encoded = label(image, [(item.choice, item.box) for item in bounds])
    trace = {}

    def _boundary(event):
        event["original"] = raw
        event["source"] = source
        if boundary is not None:
            boundary(event)

    try:
        try:
            with spans.model():
                choice, confidence = vision.choose(encoded, task, choices, ordered, evidence=evidence or None,
                                                   transport=transport, boundary=_boundary, trace=trace)
        finally:
            start, end = trace.get("construct_start"), trace.get("construct_end")
            if start is not None and end is not None:
                spans.rows.append(("request_construction", start, end))
    except ProviderFailure as exc:
        outcome = "pending" if exc.code in ("rate_limited", "quota", "timeout", "provider") else "hold"
        return _finish(spans, outcome=outcome, reason=exc.code, screen=screen, calls=1,
                       payload_bytes=trace.get("payload_bytes") or 0,
                       input_tokens=input_tokens(trace.get("usage")), sent=True, **{
                           key: base[key] for key in base if key not in ("calls", "payload_bytes", "input_tokens", "sent")})
    except TimeoutError:
        return _finish(spans, outcome="hold", reason="timeout", screen=screen, calls=1, sent=True,
                       payload_bytes=trace.get("payload_bytes") or 0, **{
                           key: base[key] for key in base if key not in ("calls", "payload_bytes", "sent")})
    base.update(calls=1, sent=True, payload_bytes=trace.get("payload_bytes") or 0,
                input_tokens=input_tokens(trace.get("usage")))
    if choice in ("hold", "wait") or choice not in choices or confidence < MIN_CONFIDENCE:
        reason = choice if choice in ("hold", "wait") else "low_confidence"
        return _finish(spans, outcome="wait" if choice == "wait" else "hold", reason=reason, screen=screen, **base)
    picked = next(item for item in bounds if item.choice == choice)
    if target is None or picked.choice != target.choice or not picked.shadow:
        reason = "rejected_choice" if target is not None and picked.choice != target.choice else "ambiguous"
        return _finish(spans, outcome="hold", reason=reason, screen=screen, **base)

    with spans.span("revalidation"):
        try:
            url = bind_tab(browser, None)
            from shadow.screenshot_loop import thread_id_of
            if bound_thread and thread_id_of(url) not in (None, bound_thread):
                return _finish(spans, outcome="hold", reason="wrong_thread", screen=screen, **base)
            if _loads(browser, "window.shadowToken()")["href"] != token["href"]:
                return _finish(spans, outcome="hold", reason="stale_observation", screen=screen, **base)
            box = _eval(browser, "window.shadowLocate(" + json.dumps(picked.shadow) + ")")
        except ObserveError:
            return _finish(spans, outcome="hold", reason="browser_detach", screen=screen, **base)
    if not isinstance(box, dict):
        return _finish(spans, outcome="hold", reason="stale_target", screen=screen, **base)
    try:
        before = _loads(browser, "window.shadowOutcome()")
    except ObserveError:
        return _finish(spans, outcome="hold", reason="bad_capture", screen=screen, **base)
    thread_before = thread_id_of(url)
    with spans.span("interaction"):
        browser.click(round(box["x"] + box["w"] / 2), round(box["y"] + box["h"] / 2))
    wait = pause if pause is not None else sleep
    after = before
    with spans.span("verification"):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            try:
                after = _loads(browser, "window.shadowOutcome()")
            except ObserveError:
                break
            if after["opened"] != before["opened"]:
                break
            wait(0.05)
    new = after["opened"][len(before["opened"]):]
    if new != [picked.shadow]:
        wrong = bool(new)
        return _finish(spans, outcome="wrong" if wrong else "hold",
                       reason="wrong_target" if wrong else "timeout", screen=screen,
                       wrong_action=wrong, action=picked.key, **base)
    url = browser.page_url()
    opened_thread = thread_id_of(url)
    if picked.kind == "row":
        proven = opened_thread == picked.shadow and after["state"] == picked.shadow
    elif picked.kind == "card":
        proven = after["state"] == "reel" and after["player"] and opened_thread == thread_before
    else:
        proven = (not after["player"] and after["state"] == thread_before and opened_thread == thread_before)
    if not proven:
        return _finish(spans, outcome="hold", reason="verification_failed", screen=screen, action=picked.key, **base)
    expects = {"row": "thread", "card": "overlay", "close": "thread"}[picked.kind]
    return _finish(spans, outcome="acted", reason=picked.kind, action=picked.key, screen=expects, **base)


def _demo():
    assert union([(0, 1), (0.5, 2), (3, 4)]) == 3
    nodes = [{"id": "1001", "x": 0, "y": 0, "w": 100, "h": 40}, {"id": "1002", "x": 0, "y": 50, "w": 100, "h": 40}]
    controls = [{"name": "Asha unread", "box": None}, {"name": "Bela", "box": None}]

    class Candidate:
        def __init__(self, key, box, center):
            self.key, self.box, self.center = key, box, center

    candidates = [Candidate("row-1", (0, 0, 100, 40), (50, 20)), Candidate("row-2", (0, 50, 100, 40), (50, 70))]
    labels = align_labels(candidates, controls, nodes)
    assert labels == {"row-1": "Asha unread", "row-2": "Bela"}
    bounds = [Bound("c1", "row-1", "row", candidates[0].box, candidates[0].center, "Inbox row 1.", "1001", labels["row-1"]),
              Bound("c2", "row-2", "row", candidates[1].box, candidates[1].center, "Inbox row 2.", "1002", labels["row-2"])]
    assert select_target(TASK_ROW, bounds).shadow == "1001"
    assert "1001" not in "\n".join(f"{item.choice}: {item.label}" for item in bounds)
    assert input_tokens({"promptTokenCount": 12}) == 12


if __name__ == "__main__":
    _demo()

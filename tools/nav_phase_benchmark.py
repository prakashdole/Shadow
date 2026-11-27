"""Paired baseline and Phase 1 navigation on the frozen synthetic inbox.

Replay is scripted. --live calls Gemini Decision 1. Reserve stays unused
unless --allow-reserve is set. Scratch output stays outside the repository.
"""

from __future__ import annotations

import argparse
import base64
from io import BytesIO
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from urllib.error import HTTPError

from shadow.agent_session import AgentSession
from shadow.chromium import find_chromium
from shadow.nav_fixture import render_call, serve
from shadow.nav_phase import (
    REEL_LOOK, UNREAD_ASK, crop_covers, subset_key,
)
from shadow.screenshot_loop import TASK_CLOSE, TASK_REEL, TASK_ROW, LiveVision, thread_id_of
from shadow.viewport import open_png


ROOT = Path(__file__).resolve().parents[1]
TASKS = {"row": TASK_ROW, "reel": TASK_REEL, "close": TASK_CLOSE}
PROCEDURES = ("baseline", "phase1")
_LINE = re.compile(r"^(c\d+): (.*) \| box (-?\d+),(-?\d+),(-?\d+),(-?\d+)$", re.M)
_UNREAD = re.compile(r"\bunread\b", re.I)


def required_successes(count):
    return (95 * count + 99) // 100


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


def row_has_dot(image, box):
    pixels = image.load()
    left, top, right, bottom = [int(value) for value in box]
    center = (top + bottom) // 2
    hits = []
    for y in range(max(0, max(top, center - 9)), min(image.height, min(bottom, center + 10))):
        for x in range(max(0, max(left, right - 44)), min(image.width, max(left, right - 13))):
            red, green, channel = pixels[x, y][:3]
            if red <= 140 and green <= 170 and channel - green >= 75:
                hits.append((x, y))
    if not 24 <= len(hits) <= 90:
        return False
    xs, ys = zip(*hits)
    return 5 <= max(xs) - min(xs) <= 12 and 5 <= max(ys) - min(ys) <= 12


def _evidence(payload):
    instructions = payload["questions"]["action"]["instructions"]
    marker = "Untrusted labels from the accessibility tree:\n"
    return instructions.split(marker, 1)[1] if marker in instructions else ""


def _criteria(payload):
    return payload["questions"]["action"]["criteria"]


def _rows(criteria):
    return [key for key in criteria if key not in ("hold", "wait", "yes", "no", "none") and "+" not in key]


def _groups(snapshot):
    groups, current = [], None
    for line in (snapshot or "").splitlines():
        if "button" in line:
            if current is not None and "[ref=" in line:
                name = line.split('"', 2)[1] if '"' in line else ""
                if "reel" in name.lower() and "photo" not in name.lower():
                    current["reels"] += 1
                else:
                    current["other"] += 1
            continue
        if "Incoming message" in line:
            current = {"side": "in", "reels": 0, "other": 0}
            groups.append(current)
        elif "Outgoing message" in line:
            current = {"side": "out", "reels": 0, "other": 0}
            groups.append(current)
    return groups


def one_latest_reel(snapshot):
    incoming = [group for group in _groups(snapshot) if group["side"] == "in"]
    if not incoming or sum(group["reels"] for group in incoming) != 1:
        return False
    last = incoming[-1]
    return last["reels"] == 1


def unread_choice(payload):
    evidence = _evidence(payload)
    try:
        image = open_png(base64.b64decode(payload["input"][0]["data"]))
    except (OSError, ValueError, KeyError):
        image = None
    marked = []
    for match in _LINE.finditer(evidence):
        key, name, *raw = match.groups()
        box = tuple(int(value) for value in raw)
        if _UNREAD.search(name) or (image is not None and row_has_dot(image, box)):
            marked.append(key)
    return subset_key(marked)


def reel_choice(payload):
    criteria = _criteria(payload)
    evidence = _evidence(payload)
    rows = _rows(criteria)
    if REEL_LOOK in payload["questions"]["action"]["instructions"]:
        return "yes" if one_latest_reel(evidence) else "hold"
    reels = [key for key in rows if "reel" in criteria[key].lower() and "photo" not in criteria[key].lower()]
    if one_latest_reel(evidence) and len(reels) == 1:
        return reels[0]
    return "hold"


def baseline_choice(payload):
    criteria = _criteria(payload)
    instructions = payload["questions"]["action"]["instructions"]
    marker = "Untrusted labels from the accessibility tree:\n"
    evidence = instructions.split(marker, 1)[1] if marker in instructions else ""
    rows = _rows(criteria)

    def blob(key):
        lines = [line for line in evidence.splitlines() if line.startswith(key + ":")]
        return criteria[key] + "\n" + "\n".join(lines)

    if "close" in instructions.lower() and len(rows) == 1:
        return rows[0]
    unread = [key for key in rows if _UNREAD.search(blob(key)) or "unread dot" in blob(key).lower()]
    if unread:
        return unread[-1]
    reels = [key for key in rows if "reel" in blob(key).lower() and "photo" not in blob(key).lower()]
    if len(reels) == 1:
        return reels[0]
    closes = [key for key in rows if "close" in blob(key).lower()]
    if len(closes) == 1:
        return closes[0]
    return "hold"


def scripted_choice(payload, procedure):
    instructions = payload["questions"]["action"]["instructions"]
    if procedure == "phase1" and UNREAD_ASK in instructions:
        return unread_choice(payload)
    if procedure == "phase1" and (REEL_LOOK in instructions or "Select the visible reel" in instructions):
        return reel_choice(payload)
    if procedure == "phase1" and "close control" in instructions.lower():
        rows = _rows(_criteria(payload))
        return rows[0] if len(rows) == 1 else "hold"
    return baseline_choice(payload)


def answer(choice, confidence=0.95):
    return {"answers": {"action": {"choice": choice, "confidence": confidence}}}


def transport_for(session, case, procedure):
    calls = {"n": 0}

    def transport(payload):
        calls["n"] += 1
        inject = case.get("inject") or "confirm"
        if inject == "timeout":
            raise TimeoutError()
        if inject == "rate":
            raise HTTPError("https://generativelanguage.googleapis.com/v1beta/decisions", 429, "rate",
                            hdrs=None, fp=BytesIO(b""))
        if inject == "low" and calls["n"] == 1:
            return answer("hold", 0.4)
        if inject == "photo_first" and calls["n"] == 1:
            criteria = _criteria(payload)
            photo = next((key for key, text in criteria.items() if "photo" in text.lower()), None)
            return answer(photo or "c1", 0.95)
        return answer(scripted_choice(payload, procedure))

    return transport


def wrong_thread(expected, opened):
    allowed = {str(item) for item in expected if str(item).isdigit()}
    return any(str(item).isdigit() and str(item) not in allowed for item in opened)


def extras_ok(expected, opened):
    index = 0
    for item in opened:
        if index < len(expected) and item == expected[index]:
            index += 1
            continue
        if item == "close-photo" or str(item).startswith("photo"):
            continue
        return False
    return index == len(expected)


def eventual_one(expect, outcome, opened, state):
    if expect.get("outcome") != outcome or wrong_thread(expect.get("opened") or [], opened):
        return False
    expected = expect.get("opened") or []
    if expect.get("outcome") != "acted":
        return opened == []
    if "state" in expect and expect["state"] != state:
        return False
    return extras_ok(expected, opened)


def eventual(case, outcome, opened, state):
    expect = case["expect"]
    options = expect["any"] if "any" in expect else [expect]
    return any(eventual_one(option, outcome, opened, state) for option in options)


def first_attempt(case, outcome, opened, state, *, wrong_clicks, second_look, recovered):
    if not eventual(case, outcome, opened, state) or wrong_clicks or second_look or recovered:
        return False
    expect = case["expect"]
    if "any" in expect:
        return any(opened == (option.get("opened") or []) for option in expect["any"])
    return opened == (expect.get("opened") or [])


def depicts(image, session):
    try:
        frame = open_png(image)
    except (OSError, ValueError):
        return False
    if frame.size != (1280, 720):
        return False
    try:
        state = json.loads(session.evaluate("window.shadowOutcome()"))
    except (TypeError, json.JSONDecodeError):
        return False
    pixel = frame.getpixel((4, 8))
    if state.get("player"):
        if max(pixel) > 80:
            return False
    elif max(abs(pixel[0] - 0x23), abs(pixel[1] - 0x32), abs(pixel[2] - 0x4d)) > 40:
        return False
    if not state.get("player"):
        probe = json.loads(session.evaluate("window.shadowProbe()"))
        for dot in probe.get("dots") or []:
            color = frame.getpixel((int(dot["x"]), int(dot["y"])))
            if not (color[2] - color[1] >= 50 and color[0] < 180):
                return False
    return True


def prepare(session, base, case):
    session.navigate(base + "/direct/inbox/")
    deadline = time.monotonic() + 8
    kind = None
    while time.monotonic() < deadline:
        try:
            kind = session.evaluate("typeof window.shadowRender")
        except Exception as exc:
            kind = f"error:{exc}"
        if kind == "function":
            break
        time.sleep(0.05)
    if kind != "function":
        try:
            url = session.page_url()
        except Exception as exc:
            url = f"error:{exc}"
        raise RuntimeError(f"fixture did not load ({kind}) at {url}")
    rendered = session.evaluate(render_call(case["screen"]))
    if rendered is not True and rendered != "true":
        raise RuntimeError(f"fixture did not render ({rendered!r})")
    if case.get("strip_hooks"):
        session.evaluate(
            "window.shadowLocate=function(){throw new Error('hook')};"
            "window.shadowToken=function(){throw new Error('hook')};"
            "window.shadowProbe=function(){throw new Error('hook')};"
            "window.shadowMutate=function(){throw new Error('hook')};")


def run_case(session, base, case, procedure, vision, gate, *, live=False):
    from shadow.nav_phase import navigate
    prepare(session, base, case)
    mutated = {"done": False}

    def boundary(event):
        if gate is not None:
            gate(event)
        if case.get("mutate") and not mutated["done"] and event.get("kind") == "full":
            session.evaluate("window.shadowMutate(" + json.dumps(case["mutate"]) + ")")
            mutated["done"] = True

    steps = case.get("journey") or [case["task"]]
    decisions = []
    for index, step in enumerate(steps):
        screen = case["screen"]
        result = navigate(
            session, vision, task=TASKS[step], procedure=procedure,
            transport=None if live else transport_for(session, case, procedure),
            boundary=boundary,
            bound_path=case.get("bound_path", screen["path"]) if index == 0 and step == "row" else None,
            bound_thread=case.get("bound_thread") if index == 0 else None,
            bound_account=case.get("bound_account", screen.get("account")) if index == 0 else None)
        decisions.append(result)
        if result.get("wrong_action") or result["outcome"] != "acted":
            break
    if case.get("strip_hooks"):
        opened, state = [], ""
        try:
            landed = thread_id_of(session.page_url())
        except Exception:
            landed = None
        outcome = decisions[-1]["outcome"] if decisions else "hold"
        if outcome == "acted" and landed:
            opened, state = [landed], landed
    else:
        try:
            observed = json.loads(session.evaluate("window.shadowOutcome()"))
        except (TypeError, json.JSONDecodeError):
            observed = {"opened": [], "state": ""}
        opened = observed.get("opened") or []
        state = observed.get("state") or ""
    outcome = decisions[-1]["outcome"] if decisions else "hold"
    if case.get("journey") and (outcome != "acted" or len(decisions) != len(steps)):
        outcome = "hold"
    wrong_clicks = sum(item.get("wrong_clicks") or 0 for item in decisions)
    second_look = any(item.get("second_look") for item in decisions)
    recovered = any(item.get("recovered") for item in decisions)
    wrong_thread_hit = wrong_thread(case["expect"].get("opened") or [], opened)
    if wrong_thread_hit or (case["expect"].get("outcome") != "acted" and opened):
        wrong_clicks = max(wrong_clicks, 1)
    ok = eventual(case, outcome, opened, state) and not wrong_thread_hit
    return {
        "id": case["id"], "procedure": procedure, "family": case["family"], "kind": case["kind"],
        "ok": ok, "first_attempt": first_attempt(
            case, outcome, opened, state, wrong_clicks=wrong_clicks, second_look=second_look, recovered=recovered),
        "outcome": outcome, "reason": decisions[-1].get("reason") if decisions else "",
        "opened": opened, "state": state,         "wrong_action": bool(wrong_clicks or wrong_thread_hit), "wrong_clicks": wrong_clicks,
        "wrong_thread": wrong_thread_hit,
        "second_look": second_look, "recovered": recovered,
        "intercepted": any(item.get("intercepted") for item in decisions),
        "calls": sum(item.get("calls") or 0 for item in decisions),
        "payload_bytes": sum(item.get("payload_bytes") or 0 for item in decisions),
        "input_tokens": None if any(item.get("input_tokens") is None for item in decisions if item.get("calls"))
        else sum(item.get("input_tokens") or 0 for item in decisions),
        "e2e_s": sum(item.get("e2e_s") or 0 for item in decisions),
        "gemini_s": sum(item.get("gemini_s") or 0 for item in decisions),
        "outside_gemini_s": sum(item.get("outside_gemini_s") or 0 for item in decisions),
        "decisions": [{key: item.get(key) for key in (
            "outcome", "reason", "wrong_action", "calls", "second_look", "recovered", "wrong_clicks",
            "intercepted", "e2e_s", "gemini_s", "outside_gemini_s")} for item in decisions],
    }


def scorecard(rows, procedure):
    items = [row for row in rows if row["procedure"] == procedure]
    actionable = [row for row in items if row["kind"] in ("act", "journey")]
    journeys = [row for row in items if row["kind"] == "journey"]
    overall = (sum(row["ok"] for row in items), len(items))
    action = (sum(row["ok"] for row in actionable), len(actionable))
    journey = (sum(row["ok"] for row in journeys), len(journeys))

    def gate(pair):
        got, total = pair
        need = required_successes(total) if total else 0
        return {"got": got, "total": total, "need": need, "pass": total > 0 and got >= need}

    families = {}
    for row in items:
        bucket = families.setdefault(row["family"], {"ok": 0, "n": 0})
        bucket["n"] += 1
        bucket["ok"] += bool(row["ok"])
    return {
        "overall": gate(overall), "actionable": gate(action), "journeys": gate(journey),
        "first_attempt": sum(row["first_attempt"] for row in items),
        "second_look_success": sum(row["ok"] and row["second_look"] for row in items),
        "recovered_success": sum(row["ok"] and row["recovered"] for row in items),
        "wrong_clicks": sum(row["wrong_clicks"] for row in items),
        "wrong_thread": sum(row["wrong_thread"] for row in items),
        "intercepted": sum(row["intercepted"] for row in items),
        "unnecessary_holds": sum(not row["ok"] and row["outcome"] == "hold" and row["kind"] in ("act", "journey")
                                 for row in items),
        "correct_holds": sum(row["ok"] and row["kind"] == "hold" for row in items),
        "calls": sum(row["calls"] for row in items),
        "input_tokens": None if any(row["input_tokens"] is None for row in items if row["calls"])
        else sum(row["input_tokens"] or 0 for row in items),
        "families": families,
    }


def environment():
    chrome = find_chromium()
    version = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=10)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, timeout=10)
    return {"os": platform.platform(), "machine": platform.machine(),
            "chrome": (version.stdout or version.stderr).strip(), "chrome_path": chrome,
            "agent_browser": "0.45.0", "model": "gemini-decision-1", "viewport": "1280x720@1",
            "min_confidence": 0.8, "commit": commit.stdout.strip()}


def load_cases(split):
    if split == "dev":
        return json.loads((ROOT / "tests/fixtures/nav_phase_dev.json").read_text())["cases"]
    document = json.loads((ROOT / "tests/fixtures/nav_phase_frozen.json").read_text())
    return [case for case in document["cases"] if case["split"] == split]


def gate_for(session, fidelity, *, check_pixels):
    def gate(event):
        fidelity["checked"] += 1
        kind = event.get("kind")
        image = event.get("image")
        if kind == "crop":
            crop = event.get("crop")
            card = event.get("card")
            if not image or not image.startswith(b"\x89PNG") or not crop_covers(crop or (), card or ()):
                fidelity["invalid"] += 1
            return
        original = event.get("original")
        if not original or not original.startswith(b"\x89PNG"):
            fidelity["invalid"] += 1
        elif check_pixels and not depicts(original, session):
            fidelity["invalid"] += 1
    return gate


def run_pairs(session, base, cases, vision, *, live=False, procedures=PROCEDURES, budget=640):
    rows, fidelity = [], {"checked": 0, "invalid": 0}
    spent = 0
    gate = gate_for(session, fidelity, check_pixels=not any(case.get("strip_hooks") for case in cases))
    for index, case in enumerate(cases):
        allowed = case.get("procedures") or procedures
        order = [name for name in (procedures if index % 2 == 0 else procedures[::-1]) if name in allowed]
        for procedure in order:
            if live and spent >= budget:
                raise SystemExit(f"Stopped. Provider budget {budget} is spent.")
            if live:
                time.sleep(1.2)
            row = run_case(session, base, case, procedure, vision, gate, live=live)
            spent += row["calls"]
            if live and row["outcome"] == "pending" and row["reason"] == "rate_limited":
                if spent >= budget:
                    raise SystemExit(f"Stopped. Provider budget {budget} is spent.")
                time.sleep(25)
                row = run_case(session, base, case, procedure, vision, gate, live=live)
                spent += row["calls"]
                row["retried_rate_limit"] = True
            rows.append(row)
            print(f"{case['id']} {procedure} {row['outcome']} {row['reason']} ok={row['ok']}", file=sys.stderr, flush=True)
    return rows, fidelity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "phase1", "reserve"), default="dev")
    parser.add_argument("--allow-reserve", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--timing", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.split == "reserve" and not args.allow_reserve:
        raise SystemExit("Reserve is untouched until the final measurement. Pass --allow-reserve.")
    cases = load_cases(args.split)
    if args.limit:
        cases = cases[:args.limit]
    if args.live:
        from dotenv import dotenv_values
        key = (os.environ.get("GEMINI_API_KEY") or dotenv_values(ROOT / ".env").get("GEMINI_API_KEY") or "").strip()
        if not key:
            raise SystemExit("GEMINI_API_KEY is unavailable")
        vision = LiveVision([key])
    else:
        vision = LiveVision(["offline"])
    started = time.monotonic()
    with serve() as base, TemporaryDirectory() as profile, AgentSession(profile, find_chromium()) as session:
        startup = time.monotonic() - started
        rows, fidelity = run_pairs(session, base, cases, vision, live=args.live)
        timing = None
        if args.timing and not args.live:
            timing = {}
            sample = next(case for case in load_cases("dev") if case["id"] == "D001")
            for procedure in PROCEDURES:
                cold = run_case(session, base, sample, procedure, vision, None)
                outside, e2e = [], []
                for _ in range(100):
                    row = run_case(session, base, sample, procedure, vision, None)
                    outside.append(row["outside_gemini_s"])
                    e2e.append(row["e2e_s"])
                timing[procedure] = {"cold_e2e_s": cold["e2e_s"], "n": 100,
                                     "outside_p50": percentile(outside, 0.50),
                                     "outside_p95": percentile(outside, 0.95),
                                     "e2e_p50": percentile(e2e, 0.50), "e2e_p95": percentile(e2e, 0.95)}
    payload = {"split": args.split, "live": args.live, "startup_s": startup, "environment": environment(),
               "fidelity": fidelity, "scorecard": {name: scorecard(rows, name) for name in PROCEDURES},
               "timing": timing, "rows": rows}
    text = json.dumps(payload, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(json.dumps({"scorecard": payload["scorecard"], "fidelity": fidelity, "timing": timing}, indent=2))


if __name__ == "__main__":
    main()

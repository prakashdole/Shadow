"""Replay the frozen navigation pilot on real Chromium.

Offline arms use a scripted Decision transport. --live calls Gemini Decision 1
on the frozen live subset. Scratch output stays out of the repository.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import json
import math
import os
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory
from urllib.error import HTTPError

from shadow.chromium import BrowserError, chromium, find_chromium
from shadow.nav_fixture import render_call, serve
from shadow.nav_observe import ARMS, navigate
from shadow.screenshot_loop import TASK_CLOSE, TASK_REEL, TASK_ROW, LiveVision
from shadow.viewport import open_png


ROOT = Path(__file__).resolve().parents[1]
TASKS = {"row": TASK_ROW, "reel": TASK_REEL, "close": TASK_CLOSE}
ORDER = ("screenshot", "playwright", "agent-browser")


def percentile(values, fraction):
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def scripted_choice(payload, inject):
    question = payload["questions"]["action"]
    criteria = question["criteria"]
    instructions = question["instructions"]
    evidence = ""
    marker = "Untrusted labels from the accessibility tree:\n"
    if marker in instructions:
        evidence = instructions.split(marker, 1)[1]
    rows = [key for key in criteria if key not in ("hold", "wait")]

    def line(key):
        return "\n".join(item for item in evidence.splitlines() if item.startswith(key + ":"))

    if inject == "wrong" and len(rows) >= 2:
        return rows[0]
    unread = [key for key in rows if "unread" in (criteria[key] + "\n" + line(key)).lower()]
    if unread:
        return unread[-1]
    reels = [key for key in rows if "reel" in line(key).lower() and "photo" not in line(key).lower()]
    if len(reels) == 1:
        return reels[0]
    media = [key for key in rows if "largest" in criteria[key].lower()]
    if len(media) == 1:
        return media[0]
    if len(rows) == 1 and any(word in (criteria[rows[0]] + line(rows[0])).lower()
                              for word in ("close", "overlay")):
        return rows[0]
    return "hold"


def transport_for(browser, case):
    inject = case.get("inject") or "confirm"
    mutate = case.get("mutate")

    def transport(payload):
        if inject == "delay":
            time.sleep(0.4)
        if inject == "timeout":
            raise TimeoutError()
        if inject == "rate":
            raise HTTPError("https://generativelanguage.googleapis.com/v1beta/decisions", 429, "rate",
                            hdrs=None, fp=BytesIO(b""))
        if mutate:
            browser.evaluate("window.shadowMutate(" + json.dumps(mutate) + ")")
        if inject == "malformed":
            return {"answers": {}}
        choice = scripted_choice(payload, inject)
        return {"answers": {"action": {"choice": choice, "confidence": 0.95}}}

    return transport


def depicts(image, browser):
    try:
        frame = open_png(image)
    except (OSError, ValueError):
        return False
    if frame.size != (1280, 720):
        return False
    state = json.loads(browser.evaluate("window.shadowOutcome()"))
    pixel = frame.getpixel((12, 20))
    if state.get("player"):
        if max(pixel) > 80:
            return False
    elif max(abs(pixel[0] - 0x23), abs(pixel[1] - 0x32), abs(pixel[2] - 0x4d)) > 40:
        return False
    if not state.get("player"):
        probe = json.loads(browser.evaluate("window.shadowProbe()"))
        for dot in probe.get("dots") or []:
            color = frame.getpixel((int(dot["x"]), int(dot["y"])))
            if not (color[2] - color[1] >= 50 and color[0] < 180):
                return False
    return True


def matches(expect, outcome, opened, state, wrong):
    if wrong:
        return False
    options = expect["any"] if "any" in expect else [expect]
    for option in options:
        if option["outcome"] != outcome:
            continue
        if option.get("opened", opened) != opened:
            continue
        if "state" in option and option["state"] != state:
            continue
        return True
    return False


def ensure_page(browser):
    """Attach to the fixture tab. The spare blank tab is never navigated or closed."""
    spare = getattr(browser, "spare_id", None)
    try:
        if (browser.session_id and browser.target_id and browser.target_id != spare
                and browser.page_url()):
            return
    except (BrowserError, OSError, KeyError):
        browser.session_id = None
    pages = [item for item in browser.browser_command("Target.getTargets")["targetInfos"]
             if item.get("type") == "page" and item.get("targetId") != spare]
    browser.session_id = None
    target = pages[0]["targetId"] if pages else browser.command(
        "Target.createTarget", url="about:blank")["targetId"]
    browser.session_id = browser.command(
        "Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
    browser.target_id = target


def prepare(browser, base, case):
    ensure_page(browser)
    browser.navigate(base + "/direct/inbox/")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if browser.evaluate("typeof window.shadowRender === 'function'"):
            break
        time.sleep(0.05)
    browser.evaluate(render_call(case["screen"]))


def blank_tab(browser):
    saved = browser.session_id
    browser.session_id = None
    try:
        browser.spare_id = browser.command(
            "Target.createTarget", url="about:blank", background=True)["targetId"]
    finally:
        browser.session_id = saved


def run_case(browser, base, case, arm, vision, gate, *, live=False):
    if case.get("detach"):
        prepare(browser, base, case)
        current = browser.target_id
        browser.browser_command("Target.closeTarget", targetId=current)
        browser.session_id = None
        browser.target_id = None
    else:
        prepare(browser, base, case)
    steps = case.get("journey") or [case["task"]]
    decisions = []
    for index, step in enumerate(steps):
        screen = case["screen"]
        result = navigate(
            browser, vision, task=TASKS[step], arm=arm,
            transport=None if live else transport_for(browser, case),
            boundary=gate, bound_path=case.get("bound_path", screen["path"]) if index == 0 else None,
            bound_thread=case.get("bound_thread") if index == 0 else None,
            bound_account=case.get("bound_account", screen.get("account")) if index == 0 else None)
        decisions.append({key: result.get(key) for key in (
            "outcome", "reason", "wrong_action", "evidence_source", "evidence_useful", "fallback_reason",
            "sent", "calls", "payload_bytes", "input_tokens", "e2e_s", "gemini_s", "outside_gemini_s",
            "stale_rejected", "spans")})
        if result.get("wrong_action") or result["outcome"] != "acted":
            break
    try:
        state = json.loads(browser.evaluate("window.shadowOutcome()"))
    except (BrowserError, TypeError, json.JSONDecodeError):
        state = {"opened": [], "state": ""}
    outcome = decisions[-1]["outcome"] if decisions else "hold"
    wrong = any(item["wrong_action"] for item in decisions)
    if case.get("journey") and outcome == "acted" and len(decisions) != len(steps):
        outcome = "hold"
    opened = state.get("opened") or []
    ok = matches(case["expect"], outcome, opened, state.get("state"), wrong)
    return {"id": case["id"], "arm": arm, "family": case["family"], "kind": case["kind"], "ok": ok,
            "outcome": outcome, "reason": decisions[-1]["reason"] if decisions else "",
            "opened": opened, "state": state.get("state"), "wrong_action": wrong,
            "calls": sum(item["calls"] or 0 for item in decisions),
            "payload_bytes": sum(item["payload_bytes"] or 0 for item in decisions),
            "input_tokens": None if any(item["input_tokens"] is None for item in decisions if item["calls"])
            else sum(item["input_tokens"] or 0 for item in decisions),
            "e2e_s": sum(item["e2e_s"] or 0 for item in decisions),
            "gemini_s": sum(item["gemini_s"] or 0 for item in decisions),
            "outside_gemini_s": sum(item["outside_gemini_s"] or 0 for item in decisions),
            "evidence_source": decisions[-1]["evidence_source"] if decisions else arm,
            "evidence_useful": any(item["evidence_useful"] for item in decisions),
            "fallback_reason": next((item["fallback_reason"] for item in decisions if item["fallback_reason"]), None),
            "sent": any(item["sent"] for item in decisions),
            "stale_rejected": sum(item["stale_rejected"] or 0 for item in decisions),
            "decisions": decisions}


def summarize(rows):
    report = {}
    for arm in ORDER:
        items = [row for row in rows if row["arm"] == arm]
        actionable = [row for row in items if row["kind"] in ("act", "journey")]
        journeys = [row for row in items if row["kind"] == "journey"]
        report[arm] = {
            "cases": len(items),
            "correct": sum(row["ok"] for row in items),
            "actionable_success": [sum(row["ok"] for row in actionable), len(actionable)],
            "journey_success": [sum(row["ok"] for row in journeys), len(journeys)],
            "wrong_actions": sum(row["wrong_action"] for row in items),
            "intercepted": sum(row.get("reason") == "rejected_choice" and row["ok"] for row in items),
            "unnecessary_holds": sum(not row["ok"] and row["outcome"] == "hold" and row["kind"] in ("act", "journey")
                                     for row in items),
            "calls": sum(row["calls"] for row in items),
            "payload_bytes": sum(row["payload_bytes"] for row in items),
            "useful": sum(row["evidence_useful"] for row in items),
            "fallbacks": sum(bool(row["fallback_reason"]) for row in items),
            "stale_rejected": sum(row["stale_rejected"] for row in items),
        }
    return report


def span_totals(row):
    totals = {}
    for decision in row.get("decisions") or []:
        for span in decision.get("spans") or []:
            totals[span["name"]] = totals.get(span["name"], 0.0) + (span["end"] - span["start"])
    return totals


def warm_times(browser, base, vision, arm):
    case = json.loads((ROOT / "tests/fixtures/nav_dev.json").read_text())["cases"][0]
    cold = run_case(browser, base, case, arm, vision, lambda _event: None)
    run_case(browser, base, case, arm, vision, lambda _event: None)
    outside, e2e, spans = [], [], []
    for _ in range(100):
        row = run_case(browser, base, case, arm, vision, lambda _event: None)
        outside.append(row["outside_gemini_s"])
        e2e.append(row["e2e_s"])
        spans.append(span_totals(row))
    names = sorted({name for item in spans for name in item})
    return {"cold_e2e_s": cold["e2e_s"], "cold_outside_s": cold["outside_gemini_s"],
            "cold_spans": span_totals(cold), "n": len(outside),
            "p50": percentile(outside, 0.50), "p95": percentile(outside, 0.95),
            "e2e_p50": percentile(e2e, 0.50), "e2e_p95": percentile(e2e, 0.95),
            "span_p50": {name: percentile([item.get(name, 0.0) for item in spans], 0.50) for name in names}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "heldout"), default="heldout")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--timing", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    document = json.loads((ROOT / "tests/fixtures" / f"nav_{args.split}.json").read_text())
    cases = [case for case in document["cases"] if not args.live or case.get("live")]
    if args.live:
        from dotenv import dotenv_values
        key = (os.environ.get("GEMINI_API_KEY") or dotenv_values(ROOT / ".env").get("GEMINI_API_KEY") or "").strip()
        if not key:
            raise SystemExit("GEMINI_API_KEY is unavailable")
        vision = LiveVision([key])
    else:
        vision = LiveVision(["offline"])
    rows, fidelity = [], {"checked": 0, "invalid": 0}
    started = time.monotonic()
    with serve() as base, TemporaryDirectory() as profile, chromium(find_chromium(), profile) as browser:
        startup = time.monotonic() - started
        blank_tab(browser)

        def gate(event):
            fidelity["checked"] += 1
            original = event.get("original")
            if not original or not depicts(original, browser):
                fidelity["invalid"] += 1

        regular = [case for case in cases if not case.get("detach")]
        for index, case in enumerate(regular):
            order = ORDER[index % 3:] + ORDER[:index % 3]
            for arm in order:
                rows.append(run_case(browser, base, case, arm, vision, gate, live=args.live))
                if args.live:
                    print(f"{case['id']} {arm} {rows[-1]['outcome']}", file=sys.stderr, flush=True)
        detached = [case for case in cases if case.get("detach")]
    if detached and not args.live:
        with serve() as base, TemporaryDirectory() as profile, chromium(find_chromium(), profile) as browser:
            blank_tab(browser)
            for case in detached:
                for arm in ORDER:
                    rows.append(run_case(browser, base, case, arm, vision, lambda _event: None))
    timing = None
    delay = None
    fallback = None
    if args.timing and not args.live:
        timing = {}
        with serve() as base, TemporaryDirectory() as profile, chromium(find_chromium(), profile) as browser:
            blank_tab(browser)
            for arm in ORDER:
                timing[arm] = warm_times(browser, base, vision, arm)
            case = json.loads((ROOT / "tests/fixtures/nav_dev.json").read_text())["cases"][0]
            base_row = run_case(browser, base, case, "screenshot", vision, lambda _event: None)
            delayed = dict(case)
            delayed["inject"] = "delay"
            delay_row = run_case(browser, base, delayed, "screenshot", vision, lambda _event: None)
            delay = {"base_e2e_s": base_row["e2e_s"], "delay_e2e_s": delay_row["e2e_s"],
                     "base_outside_s": base_row["outside_gemini_s"], "delay_outside_s": delay_row["outside_gemini_s"]}
            missing = "/tmp/shadow-agent-browser-missing"
            os.environ["SHADOW_AGENT_BROWSER"] = missing
            try:
                fallback = run_case(browser, base, case, "agent-browser", vision, lambda _event: None)
            finally:
                os.environ.pop("SHADOW_AGENT_BROWSER", None)
    gate = None
    if timing:
        base = timing["screenshot"]["p95"]
        allowance = max(0.075, 0.15 * base)
        gate = {"a_p95": base, "b_p95": timing["playwright"]["p95"],
                "c_p95": timing["agent-browser"]["p95"], "allowance_s": allowance,
                "limit_s": base + allowance, "pass": timing["agent-browser"]["p95"] <= base + allowance}
    payload = {"split": args.split, "live": args.live, "startup_s": startup, "fidelity": fidelity,
               "summary": summarize(rows), "timing": timing, "latency_gate": gate, "delay": delay,
               "fallback": None if fallback is None else {key: fallback[key] for key in (
                   "ok", "outcome", "evidence_source", "fallback_reason", "evidence_useful")},
               "rows": rows}
    text = json.dumps(payload, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(json.dumps({"summary": payload["summary"], "fidelity": fidelity, "timing": timing,
                      "latency_gate": gate, "delay": delay, "fallback": payload["fallback"]}, indent=2))


if __name__ == "__main__":
    main()

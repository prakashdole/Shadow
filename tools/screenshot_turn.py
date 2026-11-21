"""Run one screenshot draft turn. This entry point cannot send or publish a reaction."""

import argparse
import json
import os
from pathlib import Path
import time

from dotenv import load_dotenv

from shadow.chromium import BrowserError, chromium, find_chromium, profile_path
from shadow.gemini_client import gemini_keys
from shadow.model_openai import openai_api_key
from shadow.screenshot_loop import (
    TASK_CLOSE, TASK_REEL, TASK_ROW, LiveVision, act, classify, gemini_fallback,
    offer, openai_primary, open_png, run_turn, sample_player, thread_id_of, utcnow,
)
from shadow.settings import data_dir
from shadow.storage import open_storage


ROOT = Path(__file__).resolve().parents[1]


class _Top:
    """Positional choices for a read-only probe. Not a substitute for Decision 1."""

    def choose(self, png, task, choices, candidates):
        if task == TASK_ROW and "row-1" in choices:
            return "row-1", 0.95
        if task == TASK_REEL:
            cards = [item for item in candidates if item.kind == "card"]
            if cards:
                return cards[-1].key, 0.95
        if task == TASK_CLOSE:
            controls = [item for item in candidates if item.kind == "close"]
            if controls:
                return controls[-1].key, 0.95
        return "hold", 0.95


def _rss(profile):
    import subprocess
    marker = str(profile)
    total = 0
    for line in subprocess.check_output(["ps", "-ax", "-o", "rss=,command="], text=True).splitlines():
        if marker in line and "Chrome" in line:
            total += int(line.split(maxsplit=1)[0])
    return total


def _shot(browser):
    last = None
    for _ in range(8):
        try:
            return browser.screenshot()
        except BrowserError as exc:
            last = exc
            time.sleep(0.5)
    raise last


def _settle(browser, seconds):
    last, stable = None, 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        png = _shot(browser)
        if png == last:
            stable += 1
            if stable >= 2:
                return png
        else:
            last, stable = png, 0
        time.sleep(0.4)
    return _shot(browser)


def _kind(url):
    if "/accounts/login" in url or "challenge" in url:
        return "login"
    if "/direct/t/" in url:
        return "thread"
    if "/direct/" in url:
        return "inbox"
    return "other"


def _reels(browser):
    """Public reel playback only. Frames stay in memory and are not sent anywhere."""
    try:
        browser.navigate("https://www.instagram.com/reels/")
    except BrowserError:
        return {"advanced": False}
    time.sleep(2)
    frames = []
    for _ in range(4):
        try:
            frames.append(_shot(browser))
        except BrowserError:
            break
        time.sleep(0.45)
    distinct = len(set(frames))
    return {"frames": len(frames), "distinct": distinct, "advanced": distinct >= 2, "audio": False}


def live_probe(binary, profile):
    """Session reuse and a positional read. No model calls and no sends."""
    started = time.monotonic()
    with chromium(binary, profile) as browser:
        startup = round(time.monotonic() - started, 3)
        browser.navigate("https://www.instagram.com/direct/inbox/")
        shot = time.monotonic()
        first = _settle(browser, 12)
        capture = round(time.monotonic() - shot, 3)
        rss = _rss(profile)
        kind = _kind(browser.page_url())
    with chromium(binary, profile) as browser:
        browser.navigate("https://www.instagram.com/direct/inbox/")
        second = _settle(browser, 12)
        reused = _kind(browser.page_url())
        report = {"startup_s": startup, "capture_s": capture, "rss_kb": rss, "viewport": "1280x720@1",
                  "first_screen": kind, "visual_first": classify(open_png(first)),
                  "after_restart": reused, "models_called": 0, "sends": 0, "reactions": 0,
                  "identity": "not_verified"}
        if reused == "login" or kind == "login":
            report["outcome"] = "login"
            return report
        image = open_png(second)
        screen = classify(image, browser.page_url())
        rows = [item for item in offer(image, screen) if item.kind == "row"]
        shot = time.monotonic()
        browser.screenshot()
        report.update(screen=screen, rows=len(rows), one_capture_s=round(time.monotonic() - shot, 3))
        if len(rows) != 1:
            report["outcome"] = "session_only"
            report["reason"] = "needs_decision_1_for_row"
            report["reels"] = _reels(browser)
            return report
        opened = act(browser, _Top(), task=TASK_ROW, timeout=8)
        report["opened"] = opened["outcome"]
        if opened["outcome"] != "acted":
            report["outcome"] = "session_only"
            return report
        thread = thread_id_of(browser.page_url())
        report["thread"] = bool(thread)
        cards = [item for item in offer(open_png(browser.screenshot()), "thread") if item.kind == "card"]
        report["cards"] = len(cards)
        if not cards:
            report["outcome"] = "dm_opened"
            return report
        played = act(browser, _Top(), task=TASK_REEL, timeout=8)
        if played["outcome"] != "acted":
            report["outcome"] = "dm_opened"
            report["player"] = played["reason"]
            return report
        sampled = sample_player(browser.screenshot)
        closed = act(browser, _Top(), task=TASK_CLOSE, timeout=8)
        same = thread_id_of(browser.page_url()) == thread
        report.update(outcome="reel_sampled" if sampled["advanced"] and closed["outcome"] == "acted" and same else "dm_opened",
                      frames=sampled["distinct"], advanced=sampled["advanced"], audio=sampled["audio"],
                      returned=closed["outcome"] == "acted" and same)
        return report


def main(argv=None):
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="Separate SQLite directory. The observer database is refused.")
    parser.add_argument("--account", default=os.environ.get("SHADOW_ACCOUNT_ID", "local"))
    parser.add_argument("--fixture", help="Synthetic inbox URL. Omit when using --live.")
    parser.add_argument("--live", action="store_true", help="Open the persistent Instagram profile, read-only.")
    parser.add_argument("--allow-live-model", action="store_true",
                        help="Send the live viewport to the configured models. Off unless the project is approved for that data.")
    parser.add_argument("--show-draft", action="store_true", help="Print the saved draft text locally.")
    parser.add_argument("--measure", action="store_true", help="Print startup, capture and memory, then exit.")
    parser.add_argument("--browser")
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args(argv)
    directory = args.data_dir.expanduser().resolve()
    if directory == data_dir().resolve():
        parser.exit(2, "Refusing the observer database. Pass a separate --data-dir.\n")
    binary = args.browser or find_chromium()
    if args.measure:
        started = time.monotonic()
        profile = args.profile or (directory / "measure-profile")
        with chromium(binary, profile) as browser:
            startup = time.monotonic() - started
            shot = time.monotonic()
            browser.screenshot()
            capture = time.monotonic() - shot
        print(json.dumps({"startup_s": round(startup, 3), "capture_s": round(capture, 3),
                          "viewport": "1280x720@1"}))
        return
    if args.live and not args.allow_live_model:
        profile = args.profile or profile_path()
        print(json.dumps(live_probe(binary, profile)))
        return
    if not args.fixture and not args.live:
        parser.error("Pass --fixture URL or --live.")
    key = openai_api_key()
    keys = gemini_keys()
    if not key or not keys:
        parser.error("OPENAI_API_KEY (or SHADOW_OPENAI_API_KEY) and GEMINI_API_KEY are required.")
    vision = LiveVision(keys)
    profile = args.profile or (profile_path() if args.live else directory / "profile")
    with open_storage(directory) as storage, chromium(binary, profile) as browser:
        if args.fixture:
            browser.navigate(args.fixture)
            time.sleep(0.5)
        else:
            browser.navigate("https://www.instagram.com/direct/inbox/")
            time.sleep(2)
        result = run_turn(browser, vision, storage, account_id=args.account, now=utcnow(),
                          primary=openai_primary(key), fallback=gemini_fallback(keys))
        if args.show_draft and result.get("decision_id"):
            row = storage.decisions_for(result["conversation_id"])
            record = json.loads(row[-1]["record_json"])
            result = {**result, "messages": record["reply"]["messages"]}
    print(json.dumps({key: value for key, value in result.items() if key != "bubbles"}))


if __name__ == "__main__":
    main()

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
    TASK_CLOSE, TASK_NAMES, TASK_REEL, TASK_ROW, LiveVision, act, classify, gemini_fallback,
    offer, openai_primary, open_png, run_turn, sample_player, thread_id_of, utcnow,
)
from shadow.settings import data_dir
from shadow.storage import open_storage


ROOT = Path(__file__).resolve().parents[1]


class _Pick:
    """Open one verified row or card by key. This does not read display names."""

    def __init__(self, key):
        self.key = key

    def choose(self, png, task, choices, candidates):
        if task == TASK_NAMES:
            return "unique", 0.95
        if self.key in choices:
            return self.key, 0.95
        if task == TASK_CLOSE:
            closes = [item for item in candidates if item.kind == "close"]
            preferred = next((item for item in closes if item.key == "close-corner"), None)
            chosen = preferred or (closes[-1] if closes else None)
            if chosen:
                return chosen.key, 0.95
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


def _bucket(url):
    path = (url or "").split("?")[0]
    for name in ("/direct/t/", "/reel/", "/reels/", "/stories/", "/p/", "/tv/", "/direct/"):
        if name in path:
            return name.strip("/")
    return "other"


def _read_rows(browser):
    """Open a text thread and a shared media item. No model calls and no message text."""
    plain = None
    played = None
    for index in range(1, 6):
        browser.navigate("https://www.instagram.com/direct/inbox/")
        time.sleep(2.0)
        image = open_png(_shot(browser))
        screen = classify(image, browser.page_url())
        rows = [item for item in offer(image, screen) if item.kind == "row"]
        key = f"row-{index}"
        if not any(item.key == key for item in rows):
            break
        opened = act(browser, _Pick(key), task=TASK_ROW, timeout=8, names=False)
        origin = thread_id_of(browser.page_url())
        deadline = time.monotonic() + 4
        while not origin and time.monotonic() < deadline:
            time.sleep(0.3)
            origin = thread_id_of(browser.page_url())
        image = open_png(_shot(browser))
        current = classify(image, browser.page_url())
        cards = [item for item in offer(image, "thread") if item.kind == "card"]
        media = [item for item in cards if item.key.startswith("media-")]
        text = [item for item in cards if item.key.startswith("card-")]
        if plain is None and origin and current == "thread" and len(text) >= 3 and not media:
            plain = {"row": index, "bands": len(text), "opened": opened["outcome"]}
        if played is None and media and origin:
            media.sort(key=lambda item: (item.box[2] - item.box[0]) * (item.box[3] - item.box[1]), reverse=True)
            started = act(browser, _Pick(media[0].key), task=TASK_REEL, timeout=8, names=False)
            player = _bucket(browser.page_url())
            if player in ("p", "reel", "reels", "tv") or started["outcome"] == "acted":
                sampled = sample_player(browser.screenshot)
                closed = act(browser, _Pick("close-corner"), task=TASK_CLOSE, timeout=8, names=False)
                same = thread_id_of(browser.page_url()) == origin
                played = {"row": index, "player": player, "opened": started["outcome"],
                          "frames": sampled["distinct"], "advanced": sampled["advanced"],
                          "audio": sampled["audio"],
                          "returned": closed["outcome"] == "acted" and same}
        if plain and played:
            break
    return plain, played


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
        report.update(screen=screen, rows=len(rows), one_capture_s=round(time.monotonic() - shot, 3),
                      identity="row_index")
        if screen != "inbox" or not rows:
            report["outcome"] = "session_only"
            report["reason"] = screen
            return report
        plain, played = _read_rows(browser)
        report["plain"] = plain
        report["shared_media"] = played
        if plain and played and played.get("advanced") and played.get("returned"):
            report["outcome"] = "reel_sampled"
        elif plain or played:
            report["outcome"] = "dm_opened"
        else:
            report["outcome"] = "session_only"
            report["reason"] = "no_thread"
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

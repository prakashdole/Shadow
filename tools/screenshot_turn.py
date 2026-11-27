"""Run one screenshot turn with optional delivery to an exact thread."""

import argparse
import json
import os
from pathlib import Path
import re
import time

from dotenv import load_dotenv

from shadow.chromium import BrowserError, chromium, find_chromium, profile_path
from shadow.chromium_send import deliver
from shadow.gemini_client import gemini_keys
from shadow.model_openai import openai_api_key
from shadow.screenshot_loop import (
    TASK_CLOSE, TASK_NAMES, TASK_REEL, TASK_ROW, LiveVision, act, claim_inbox_retry, classify,
    finish_inbox_turn, gemini_fallback, offer, openai_primary, open_png, run_check, run_turn,
    sample_player, thread_id_of, utcnow, within_hours,
)
from shadow.settings import data_dir
from shadow.storage import StorageLocked, open_storage


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
    marker = f"--user-data-dir={Path(profile).expanduser().resolve()}"
    total = 0
    for line in subprocess.check_output(["ps", "-ax", "-o", "rss=,command="], text=True).splitlines():
        executable, separator, _ = line.partition(marker)
        if separator and any(name in executable.lower() for name in ("chrome", "chromium")):
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
        if media and origin and (played or {}).get("player") not in ("reel", "reels"):
            media.sort(key=lambda item: (item.box[2] - item.box[0]) * (item.box[3] - item.box[1]), reverse=True)
            started = act(browser, _Pick(media[0].key), task=TASK_REEL, timeout=8, names=False)
            player = _bucket(browser.page_url())
            if player in ("p", "reel", "reels", "tv") or started["outcome"] == "acted":
                sampled = sample_player(browser.screenshot)
                closed = act(browser, _Pick("close-corner"), task=TASK_CLOSE, timeout=8, names=False)
                same = thread_id_of(browser.page_url()) == origin
                candidate = {"row": index, "player": player, "opened": started["outcome"],
                             "frames": sampled["distinct"], "advanced": sampled["advanced"],
                             "audio": sampled["audio"],
                             "returned": closed["outcome"] == "acted" and same}
                played = _prefer(played, candidate)
        if plain and played and played.get("player") in ("reel", "reels") and played.get("returned"):
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
        first_url = browser.page_url()
        kind = _kind(first_url)
    with chromium(binary, profile) as browser:
        browser.navigate("https://www.instagram.com/direct/inbox/")
        second = _settle(browser, 12)
        reused = _kind(browser.page_url())
        report = {"startup_s": startup, "capture_s": capture, "rss_kb": rss, "viewport": "1280x720@1",
                  "first_screen": kind, "visual_first": classify(open_png(first), first_url),
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
        report["outcome"] = live_outcome(plain, played)
        if report["outcome"] == "session_only":
            report["reason"] = "no_thread"
        return report


def _prefer(current, candidate):
    if current is None:
        return candidate
    if candidate["player"] in ("reel", "reels") and candidate.get("returned"):
        return candidate
    if not current.get("returned") and candidate.get("returned"):
        return candidate
    return current


def live_outcome(plain, played):
    """A post viewer is not the Reels player. Only a reel route can clear that check."""
    route = (played or {}).get("player")
    if plain and played and played.get("advanced") and played.get("returned") and route in ("reel", "reels"):
        return "reel_sampled"
    if plain or played:
        return "dm_opened"
    return "session_only"


def main(argv=None):
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="Separate SQLite directory. The observer database is refused.")
    parser.add_argument("--account", default=os.environ.get("SHADOW_ACCOUNT_ID", "local"))
    parser.add_argument("--fixture", help="Synthetic inbox URL. Omit when using --live.")
    parser.add_argument("--live", action="store_true", help="Open the persistent Instagram profile, read-only.")
    parser.add_argument("--allow-live-model", action="store_true",
                        help="Process live chat content with the configured models. Use provider projects approved for that data.")
    parser.add_argument("--thread", help="Numeric Instagram thread ID for an owner-selected old or current conversation.")
    parser.add_argument("--send", action="store_true", help="Deliver a verified draft to the specified thread.")
    parser.add_argument("--deliver", action="store_true",
                        help="Scheduled inbox check. Reply to eligible unread chats inside India daytime.")
    parser.add_argument("--allow-thread", action="append", default=[], dest="allow_threads",
                        help="Qualification only: transcribe and send this numeric thread and no others.")
    parser.add_argument("--send-text", help="Deliver one owner-directed message to the specified thread.")
    parser.add_argument("--resume-login", action="store_true", help="Clear a login pause after verifying a signed-in inbox.")
    parser.add_argument("--show-draft", action="store_true", help="Print the saved draft text locally.")
    parser.add_argument("--measure", action="store_true", help="Print startup, capture and memory, then exit.")
    parser.add_argument("--browser")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--observe", choices=("screenshot", "playwright", "agent-browser", "agent-nav"),
                        help="Opt-in navigation evidence. Omit to keep the screenshot act() path.")
    args = parser.parse_args(argv)
    if args.fixture and args.live:
        parser.error("Choose either --fixture or --live.")
    if args.allow_live_model and not args.live:
        parser.error("--allow-live-model requires --live.")
    if args.thread and (not args.live or not re.fullmatch(r"[0-9]+", args.thread)):
        parser.error("--thread requires --live and an exact numeric thread ID.")
    if (args.send or args.send_text is not None) and (not args.thread or args.measure):
        parser.error("Delivery requires --live and --thread.")
    if args.deliver and (not args.live or not args.allow_live_model or args.thread or args.send
                         or args.send_text is not None or args.measure):
        parser.error("--deliver requires --live --allow-live-model and selects the inbox itself.")
    if args.allow_threads and not args.deliver:
        parser.error("--allow-thread is only valid with --deliver.")
    if any(not re.fullmatch(r"[0-9]+", item) for item in args.allow_threads):
        parser.error("--allow-thread requires an exact numeric thread ID.")
    if args.send and (not args.allow_live_model or args.send_text is not None):
        parser.error("--send uses a model draft and requires --allow-live-model.")
    if args.resume_login and (not args.live or args.allow_live_model or args.thread or args.measure):
        parser.error("--resume-login requires a read-only --live inbox check.")
    os.umask(0o077)
    directory = args.data_dir.expanduser().resolve()
    if directory == data_dir().resolve():
        parser.exit(2, "Refusing the observer database. Pass a separate --data-dir.\n")
    binary = args.browser or find_chromium()
    if args.send_text is not None:
        with open_storage(directory) as storage, chromium(binary, args.profile or profile_path()) as browser:
            runtime = storage.get_runtime() or {}
            if runtime.get("pause_reason") or runtime.get("account_id") not in (None, "", args.account):
                parser.error("Delivery is paused or bound to another account.")
            conversation = storage.get_or_create_conversation(args.account, args.thread)
            browser.navigate(f"https://www.instagram.com/direct/t/{args.thread}/")
            _settle(browser, 8)
            decision = storage.add_decision(conversation_id=conversation, source_revision=0,
                                            record_json=json.dumps({"owner_command": True}), versions_json="{}")
            result = deliver(browser, storage, thread=args.thread, decision=decision,
                             conversation=conversation, revision=0, text=args.send_text,
                             authority="owner_command")
        print(json.dumps(result))
        return
    if args.measure:
        started = time.monotonic()
        profile = args.profile or (directory / "measure-profile")
        with chromium(binary, profile) as browser:
            startup = time.monotonic() - started
            shot = time.monotonic()
            browser.screenshot()
            capture = time.monotonic() - shot
            rss = _rss(profile)
        print(json.dumps({"startup_s": round(startup, 3), "capture_s": round(capture, 3),
                          "rss_kb": rss, "viewport": "1280x720@1"}))
        return
    if args.live and not args.allow_live_model:
        profile = args.profile or profile_path()
        if not args.thread and not args.resume_login:
            print(json.dumps(live_probe(binary, profile)))
            return
        with chromium(binary, profile) as browser:
            url = (f"https://www.instagram.com/direct/t/{args.thread}/" if args.thread
                   else "https://www.instagram.com/direct/inbox/")
            browser.navigate(url)
            image = open_png(_settle(browser, 8))
            current = browser.page_url()
            screen = classify(image, current)
        report = {"screen": screen, "models_called": 0, "sends": 0, "reactions": 0}
        if args.thread:
            report["verified_thread"] = screen == "thread" and thread_id_of(current) == args.thread
        elif screen == "inbox" and _kind(current) == "inbox":
            with open_storage(directory) as storage:
                state = storage.get_runtime() or {}
                if state.get("account_id") == args.account and state.get("pause_reason") == "login_required":
                    storage.set_runtime(account_id=state["account_id"], mode=state["mode"],
                                        autonomy_scope=state["autonomy_scope"], config_digest=state["config_digest"])
                    report["resumed"] = True
        print(json.dumps(report))
        return
    if not args.fixture and not args.live:
        parser.error("Pass --fixture URL or --live.")
    key = openai_api_key()
    keys = gemini_keys()
    if not key or not keys:
        parser.error("OPENAI_API_KEY (or SHADOW_OPENAI_API_KEY) and GEMINI_API_KEY are required.")
    vision = LiveVision(keys)
    profile = args.profile or (profile_path() if args.live else directory / "profile")
    if args.deliver:
        if not within_hours(utcnow()):
            print(json.dumps({"outcome": "outside_hours", "sends": 0, "reactions": 0}))
            return
        try:
            opened = open_storage(directory)
        except StorageLocked:
            print(json.dumps({"outcome": "overlap", "sends": 0, "reactions": 0}))
            return
        with opened as storage, chromium(binary, profile) as browser:
            result = run_check(browser, vision, storage, account_id=args.account, now=utcnow(),
                               primary=openai_primary(key), fallback=gemini_fallback(keys),
                               deliver=deliver, allow_threads=args.allow_threads or None,
                               observe=args.observe)
        print(json.dumps({key: value for key, value in result.items() if key != "bubbles"}))
        return
    if args.observe == "agent-nav":
        from shadow.agent_session import AgentSession
        browser_cm = AgentSession(profile, binary)
    else:
        browser_cm = chromium(binary, profile)
    with open_storage(directory) as storage, browser_cm as browser:
        auto = args.live and not args.thread
        state = storage.get_runtime() or {}
        tracking = auto and not state.get("pause_reason") and state.get("account_id") in (None, "", args.account)
        expected = args.thread or (claim_inbox_retry(storage, args.account) if tracking else None)
        if args.fixture:
            browser.navigate(args.fixture)
            time.sleep(0.5)
        else:
            url = (f"https://www.instagram.com/direct/t/{expected}/" if expected
                   else "https://www.instagram.com/direct/inbox/")
            browser.navigate(url)
            time.sleep(2)
        result = run_turn(browser, vision, storage, account_id=args.account, now=utcnow(),
                          primary=openai_primary(key), fallback=gemini_fallback(keys),
                          expected_thread_id=expected, track_inbox=tracking and expected is None,
                          observe=args.observe)
        if tracking:
            finish_inbox_turn(storage, args.account, result)
        if (args.show_draft or args.send) and result.get("decision_id"):
            row = storage.decisions_for(result["conversation_id"])
            saved = next(entry for entry in row if entry["id"] == result["decision_id"])
            messages = json.loads(saved["record_json"])["reply"]["messages"]
            if args.send and result["outcome"] == "draft":
                result.update(deliver(browser, storage, thread=args.thread, decision=result["decision_id"],
                                      conversation=result["conversation_id"], revision=result["revision"],
                                      text=messages[0], authority="owner_approved"))
            if args.show_draft:
                result["messages"] = messages
    print(json.dumps({key: value for key, value in result.items() if key != "bubbles"}))


if __name__ == "__main__":
    main()

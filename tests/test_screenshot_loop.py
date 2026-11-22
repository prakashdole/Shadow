"""Offline and fixture checks for the screenshot draft loop."""

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from shadow.chromium import BrowserError
from shadow.gemini_client import ProviderFailure, generate
from shadow.screenshot_loop import (
    HEADER, TASK_CLOSE, TASK_NAMES, TASK_REEL, TASK_ROW, act, claim_inbox_retry, claim_next,
    draft_and_save, evidence_problem, fingerprint, finish_inbox_turn, note_opened, parse_transcript,
    run_check, run_turn, sample_interval, sample_player, turn_change, within_hours,
)
from shadow.storage import open_storage
from shadow.targets import classify, offer, thread_box
from shadow.viewport import crop_resize, open_png, png_bytes, region_stable

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


def bubble(order, side, text, media=None, unreadable=False, clipped=False):
    return {"order": order, "side": side, "text": text, "quote": None, "media": media,
            "unreadable": unreadable, "clipped": clipped}


def reply(text="haan"):
    return json.dumps({"schema_version": 1, "action": "reply", "reason_code": "ok",
                       "messages": [text], "evidence_event_ids": [], "used_memory_ids": []})


def paint(lines, *, column="inbox"):
    """Dark runs stand in for text. The default bitmap font is too thin to segment."""
    image = Image.new("RGB", (1280, 720), "#f6f8fa")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1280, 56), fill="#23324d")
    x = 110 if column == "inbox" else 400
    y = 110
    for line in lines:
        draw.rectangle((x, y, x + max(90, 12 * len(line)), y + 44), fill="#182134")
        y += 72
    return image


class Shots:
    def __init__(self, images, url="http://127.0.0.1/"):
        self.images = [png_bytes(image) if not isinstance(image, bytes) else image for image in images]
        self.n = 0
        self.clicks = []
        self.url = url

    def screenshot(self):
        blob = self.images[min(self.n, len(self.images) - 1)]
        self.n += 1
        return blob

    def click(self, x, y):
        self.clicks.append((x, y))

    def scroll(self, x, y, dy):
        self.clicks.append(("scroll", x, y, dy))

    def page_url(self):
        return self.url


class Vision:
    def __init__(self, choice="row-1", names="unique", transcript=None, summary=None):
        self.choice, self.names = choice, names
        self.transcript = transcript or [bubble(1, "left", "yaar kal milte hai?")]
        self.summary = summary or {"subject": "synthetic", "action": "holds a sign",
                                    "on_screen_text": "", "speech": "nope", "missing": ""}
        self.calls = []
        self.described = 0

    def choose(self, png, task, choices, candidates):
        self.calls.append(task)
        if task == TASK_NAMES:
            return self.names, 0.95
        if self.choice not in choices:
            return "hold", 0.95
        return self.choice, 0.95

    def transcribe(self, png):
        return {"bubbles": self.transcript, "incomplete": False}

    def describe(self, frames, caption, audio=False):
        self.described += 1
        return dict(self.summary)


def store():
    directory = TemporaryDirectory()
    return directory, open_storage(directory.name)


class MappingTests(unittest.TestCase):
    def test_crop_and_resize_map_once(self):
        image = Image.new("RGB", (1280, 720), "white")
        image.putpixel((460, 225), (255, 0, 0))
        _view, transform = crop_resize(image, (350, 150, 560, 280), (420, 260))
        self.assertEqual(transform.to_source(220, 150), (460, 225))
        twice = transform.to_source(*transform.to_source(220, 150))
        self.assertNotEqual(twice, (460, 225))
        self.assertTrue(region_stable(image, image, (400, 200, 520, 250)))

    def test_labels_do_not_touch_the_original(self):
        from shadow.viewport import label
        image = paint(["Alice"])
        before = png_bytes(image)
        marked = label(image, [("row-1", (100, 110, 200, 140))])
        self.assertEqual(png_bytes(image), before)
        self.assertNotEqual(marked, before)


class ScreenTests(unittest.TestCase):
    def test_duplicate_names_do_not_click(self):
        image = paint(["Sam Account", "Sam Account"])
        self.assertEqual(classify(image, "http://127.0.0.1/"), "inbox")
        self.assertGreaterEqual(len(offer(image, "inbox")), 2)
        browser = Shots([image])
        result = act(browser, Vision(names="duplicate"), task=TASK_ROW)
        self.assertEqual(result["outcome"], "hold")
        self.assertEqual(result["reason"], "ambiguous_duplicate")
        self.assertEqual(browser.clicks, [])

    def test_moved_target_is_not_clicked(self):
        browser = Shots([paint(["Alice Example"]), paint(["Bob Example"])])
        result = act(browser, Vision(), task=TASK_ROW)
        self.assertEqual(result["reason"], "target_moved")
        self.assertEqual(browser.clicks, [])

    def test_header_only_and_blank_wait(self):
        loading = paint(["Alice"], column="thread")
        browser = Shots([loading], url="https://www.instagram.com/direct/t/101/")
        vision = Vision()
        result = act(browser, vision, task=TASK_ROW)
        self.assertEqual(result["outcome"], "wait")
        self.assertEqual(result["reason"], "header_only")
        self.assertEqual(vision.calls, [])
        blank = Shots([Image.new("RGB", (1280, 720), "white")])
        self.assertEqual(act(blank, vision, task=TASK_ROW)["reason"], "blank")

    def test_overlay_hides_inbox_rows(self):
        image = Image.new("RGB", (1280, 720), "#20283d")
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 400, 180, 444), fill="#f4f6fb")
        self.assertEqual(classify(image), "overlay")
        kinds = {candidate.kind for candidate in offer(image, "overlay")}
        self.assertEqual(kinds, {"close"})

    def test_dark_inbox_is_not_an_overlay(self):
        image = Image.new("RGB", (1280, 720), "#121212")
        draw = ImageDraw.Draw(image)
        draw.rectangle((96, 300, 340, 348), fill="#d0d4dc")
        draw.rectangle((96, 380, 340, 428), fill="#d0d4dc")
        draw.rectangle((820, 400, 960, 440), fill="#4c8dff")
        self.assertEqual(classify(image), "inbox")
        self.assertEqual(len(offer(image, "inbox")), 2)

    def test_inbox_with_welcome_panel_still_offers_rows(self):
        image = paint(["Old chat", "Shared reel"])
        draw = ImageDraw.Draw(image)
        for top in (230, 295, 360):
            draw.rectangle((715, top, 955, top + 30), fill="#182134")
        screen = classify(image, "https://www.instagram.com/direct/inbox/?entry=direct")
        self.assertEqual(screen, "inbox")
        self.assertEqual([candidate.key for candidate in offer(image, screen)], ["row-1", "row-2"])

    def test_shared_post_player_uses_overlay_controls(self):
        image = paint(["Old chat", "Shared video"])
        draw = ImageDraw.Draw(image)
        draw.rectangle((715, 120, 1060, 560), fill="#182134")
        url = "https://www.instagram.com/p/example/"
        self.assertEqual(classify(image, url), "overlay")
        self.assertEqual(classify(Image.new("RGB", image.size, "white"), url), "blank")
        browser = Shots([image], url=url)
        self.assertEqual(act(browser, Vision(), task=TASK_CLOSE)["reason"], "no_target")
        self.assertEqual(browser.clicks, [])

        draw.line((1232, 21, 1244, 33), fill="white", width=3)
        draw.line((1232, 33, 1244, 21), fill="white", width=3)
        thread = paint(["Old chat", "Shared video"])
        for top in (230, 295, 360):
            ImageDraw.Draw(thread).rectangle((715, top, 955, top + 30), fill="#182134")

        class PostPlayer(Shots):
            def click(self, x, y):
                super().click(x, y)
                self.url = "https://www.instagram.com/direct/t/101/"

        browser = PostPlayer([image, image, thread], url=url)
        result = act(browser, Vision(choice="close-corner"), task=TASK_CLOSE)
        self.assertEqual(result["outcome"], "acted")
        self.assertEqual(browser.clicks, [(1238, 27)])

    def test_a_large_media_block_is_a_loaded_thread(self):
        image = Image.new("RGB", (1280, 720), "#121212")
        draw = ImageDraw.Draw(image)
        draw.rectangle((96, 300, 340, 348), fill="#d0d4dc")
        draw.rectangle((96, 380, 340, 428), fill="#d0d4dc")
        draw.rectangle((700, 140, 940, 460), fill="#d0d4dc")
        self.assertEqual(classify(image, "https://www.instagram.com/direct/t/101/"), "thread")
        cards = [item for item in offer(image, "thread") if item.key.startswith("media-")]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].expects, "overlay")

    def test_transcript_crop_excludes_a_wide_inbox(self):
        image = Image.new("RGB", (1280, 720), "#121212")
        draw = ImageDraw.Draw(image)
        draw.rectangle((96, 300, 430, 348), fill="#d0d4dc")
        draw.rectangle((96, 380, 430, 428), fill="#d0d4dc")
        draw.rectangle((700, 200, 900, 244), fill="#d0d4dc")
        left, _top, right, _bottom = thread_box(image)
        self.assertGreaterEqual(left, 430)
        self.assertLess(left, 700)
        self.assertEqual(right, 1260)

    def test_low_confidence_holds(self):
        image = paint(["Alice Example", "Bob Example"])
        browser = Shots([image, image])

        class Unsure(Vision):
            def choose(self, png, task, choices, candidates):
                if task == TASK_NAMES:
                    return "unique", 0.95
                return "row-1", 0.4

        result = act(browser, Unsure(), task=TASK_ROW)
        self.assertEqual(result["reason"], "low_confidence")
        self.assertEqual(browser.clicks, [])


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.storage = store()
        self.addCleanup(self.storage.close)
        self.addCleanup(self.directory.cleanup)

    def _save(self, bubbles, primary, media=None, fallback=None, reread=None):
        return draft_and_save(self.storage, account_id="acct", thread_id="101", bubbles=bubbles,
                              media=media, primary=primary, fallback=fallback, now=NOW, reread=reread)

    def test_repeated_bubbles_and_retry(self):
        bubbles = [bubble(1, "left", "ok"), bubble(2, "left", "ok")]
        self.assertNotEqual(fingerprint(bubbles), fingerprint([bubble(1, "left", "ok")]))
        calls = []

        def primary(system, user):
            calls.append(user)
            return reply(), {"model": "gpt-4o-mini", "usage": {"total": 3}, "latency_ms": 5}

        first = self._save(bubbles, primary)
        second = self._save(bubbles, primary)
        self.assertEqual(first["outcome"], "draft")
        self.assertEqual(second["outcome"], "existing")
        self.assertEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(len(calls), 1)
        record = json.loads(self.storage.decisions_for(first["conversation_id"])[0]["record_json"])
        self.assertEqual([item["text"] for item in record["bubbles"]], ["ok", "ok"])
        self.assertEqual(self.storage.outbox_items(), [])

    def test_uncertain_unreadable_and_stalled_hold_without_the_model(self):
        def boom(system, user):
            raise AssertionError("model")

        uncertain = self._save([bubble(1, "unknown", "maybe")], boom)
        unreadable = self._save([bubble(1, "left", "₹", unreadable=True)], boom)
        stalled = self._save([bubble(1, "left", "dekho", media="reel")], boom,
                             media={"advanced": False, "stalled": True, "audio": False})
        self.assertEqual(uncertain["reason"], "uncertain_sender")
        self.assertEqual(unreadable["reason"], "unreadable")
        self.assertEqual(stalled["reason"], "media_incomplete")
        self.assertEqual(evidence_problem([bubble(1, "left", "hi")], None), None)

    def test_new_message_discards_the_draft(self):
        bubbles = [bubble(1, "left", "hi")]
        later = bubbles + [bubble(2, "left", "aur ek")]

        def primary(system, user):
            return reply("reply that must not be stored"), {"model": "gpt-4o-mini", "usage": {}}

        result = self._save(bubbles, primary, reread=lambda: later)
        self.assertEqual(result["outcome"], "stale")
        self.assertEqual(self.storage.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_owner_reply_supersedes_old_incoming_turn(self):
        before = [bubble(1, "left", "hi")]
        first = self._save(before, lambda *_: (reply(), {}))
        result = self._save(before + [bubble(2, "right", "answered")],
                            lambda *_: self.fail("reply model called on an answered turn"))
        self.assertEqual(result["reason"], "owner_answered")
        rows = self.storage.decisions_for(first["conversation_id"])
        self.assertEqual(rows[0]["status"], "superseded")

    def test_replied_to_reel_is_not_reused_for_unrelated_followup(self):
        bubbles = [bubble(1, "left", "old reel", "reel"), bubble(2, "right", "saw it"),
                   bubble(3, "left", "kal free hai?")]
        result = self._save(bubbles, lambda *_: (reply(), {}))
        self.assertEqual(result["outcome"], "draft")

    def test_requested_thread_is_checked_before_transcribing(self):
        image = paint(["First message in the open conversation", "Second message in the open conversation",
                       "Third message in the open conversation"], column="thread")
        browser = Shots([image], url="https://www.instagram.com/direct/t/101/")

        class NeverTranscribe(Vision):
            def transcribe(self, png):
                raise AssertionError("transcribed the wrong thread")

        result = run_turn(browser, NeverTranscribe(), self.storage, account_id="acct", now=NOW,
                          primary=lambda *_: self.fail("reply model called for the wrong thread"),
                          fallback=None, expected_thread_id="202")
        self.assertEqual(result["reason"], "wrong_thread")

    def test_thread_switch_during_draft_discards_reply(self):
        image = paint(["First message in the open conversation", "Second message in the open conversation",
                       "Third message in the open conversation"], column="thread")
        browser = Shots([image], url="https://www.instagram.com/direct/t/101/")

        def primary(*_):
            browser.url = "https://www.instagram.com/direct/t/202/"
            return reply(), {}

        result = run_turn(browser, Vision(), self.storage, account_id="acct", now=NOW,
                          primary=primary, fallback=None)
        self.assertEqual(result["reason"], "wrong_thread")
        self.assertEqual(self.storage.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_unchanged_conversation_needs_only_one_transcription(self):
        image = paint(["First message in the open conversation", "Second message in the open conversation",
                       "Third message in the open conversation"], column="thread")
        browser = Shots([image], url="https://www.instagram.com/direct/t/101/")

        class Once(Vision):
            def transcribe(self, png):
                self.calls.append("transcribe")
                return super().transcribe(png)

        vision = Once()
        result = run_turn(browser, vision, self.storage, account_id="acct", now=NOW,
                          primary=lambda *_: (reply(), {}), fallback=None)
        self.assertEqual(result["outcome"], "draft")
        self.assertEqual(vision.calls, ["transcribe"])

    def test_incomplete_transcript_waits_without_drafting(self):
        image = paint(["First message in the open conversation", "Second message in the open conversation",
                       "Third message in the open conversation"], column="thread")
        browser = Shots([image], url="https://www.instagram.com/direct/t/101/")

        class IncompleteVision(Vision):
            def transcribe(self, png):
                return {**super().transcribe(png), "incomplete": True}

        result = run_turn(browser, IncompleteVision(), self.storage, account_id="acct", now=NOW,
                          primary=lambda *_: self.fail("reply model called on an incomplete turn"),
                          fallback=None)
        self.assertEqual(result["reason"], "incomplete_transcript")

    def test_hold_does_not_fall_back(self):
        def primary(system, user):
            return json.dumps({"schema_version": 1, "action": "hold", "reason_code": "need_detail",
                               "messages": [], "evidence_event_ids": [], "used_memory_ids": []}), {"model": "gpt-4o-mini"}

        def fallback(system, user):
            raise AssertionError("fallback")

        result = self._save([bubble(1, "left", "hi")], primary, fallback=fallback)
        self.assertEqual(result["outcome"], "hold")
        self.assertEqual(result["used"], "primary")

    def test_transport_failure_uses_fallback_once(self):
        def primary(system, user):
            raise ProviderFailure("provider")

        def fallback(system, user):
            return reply("theek hai"), {"model": "gemini-3.5-flash-lite", "usage": {"total_tokens": 8}, "latency_ms": 12}

        result = self._save([bubble(1, "left", "kya scene hai?")], primary, fallback=fallback)
        self.assertEqual(result["outcome"], "draft")
        self.assertEqual(result["used"], "fallback")
        record = json.loads(self.storage.decisions_for(result["conversation_id"])[0]["record_json"])
        self.assertEqual(record["reply"]["messages"], ["theek hai"])

    def test_rate_limit_pause_and_restart(self):
        def limited(system, user):
            raise ProviderFailure("rate_limited")

        pending = self._save([bubble(1, "left", "hi")], limited, fallback=None)
        self.assertEqual(pending["outcome"], "pending")
        self.assertEqual(self.storage.get_work(pending["conversation_id"])["state"], "pending")
        self.assertEqual(self.storage.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)
        self.storage.set_runtime(account_id="acct", pause_reason="owner_pause")
        self.assertEqual(self._save([bubble(1, "left", "hi")], limited)["outcome"], "paused")
        self.storage.set_runtime(account_id="acct")
        saved = self._save([bubble(1, "left", "hello")], lambda system, user: (reply(), {"model": "gpt-4o-mini"}))
        directory = self.directory.name
        conversation = saved["conversation_id"]
        self.storage.close()
        with open_storage(directory) as restarted:
            again = draft_and_save(restarted, account_id="acct", thread_id="101",
                                   bubbles=[bubble(1, "left", "hello")], media=None,
                                   primary=lambda system, user: (_ for _ in ()).throw(AssertionError("again")),
                                   fallback=None, now=NOW)
            self.assertEqual(again["outcome"], "existing")
            newer = draft_and_save(restarted, account_id="acct", thread_id="101",
                                   bubbles=[bubble(1, "left", "hello"), bubble(2, "left", "aur")], media=None,
                                   primary=lambda system, user: (reply("naya"), {"model": "gpt-4o-mini"}),
                                   fallback=None, now=NOW)
            self.assertEqual(newer["outcome"], "draft")
            rows = {row["id"]: row["status"] for row in restarted.decisions_for(conversation)}
            self.assertEqual(rows[saved["decision_id"]], "superseded")
            self.assertNotEqual(newer["decision_id"], saved["decision_id"])

    def test_login_pauses_and_timeout_recaptures(self):
        image = Image.new("RGB", (1280, 720), "white")
        browser = Shots([image], url="https://www.instagram.com/accounts/login/")
        result = run_turn(browser, Vision(), self.storage, account_id="acct", now=NOW,
                          primary=lambda *args: (_ for _ in ()).throw(AssertionError()), fallback=None)
        self.assertEqual(result["reason"], "login_required")
        self.assertEqual(self.storage.get_runtime()["pause_reason"], "login_required")
        self.storage.set_runtime(account_id="acct")
        stuck = paint(["Alice Example", "Bob Example"])
        shots = Shots([stuck, stuck, stuck, stuck])
        timed = act(shots, Vision(), task=TASK_ROW, timeout=0.2, sleep=lambda _seconds: None)
        self.assertEqual(timed["reason"], "timeout")
        self.assertTrue(timed["recapture"])

    def test_quote_and_react_are_not_actions(self):
        image = paint(["Alice Example"])
        keys = {candidate.key for candidate in offer(image, classify(image))}
        self.assertTrue(keys.isdisjoint({"react", "send", "quote"}))


class SampleTests(unittest.TestCase):
    def test_intervals_and_stall(self):
        events = [(0, "a"), (0.5, "b"), (1.0, "c"), (1.5, "d"), (2.0, "e")]
        self.assertGreater(len(sample_interval(events, 1)), len(sample_interval(events, 4)))
        late = [(0, "setup"), (5.0, "reveal")]
        self.assertIn("reveal", {token for _, token in sample_interval(late, 1, window=6)})
        self.assertNotIn("reveal", {token for _, token in sample_interval(late, 4, window=3)})

        class Clock:
            def __init__(self, frames):
                self.t = 0.0
                self.frames = frames

            def __call__(self):
                return self.t

            def sleep(self, seconds):
                self.t += seconds

            def grab(self):
                token = self.frames[0][1]
                for stamp, value in self.frames:
                    if stamp <= self.t + 1e-9:
                        token = value
                return token.encode()

        fast = Clock([(0, "a"), (0.2, "b"), (0.7, "c"), (1.2, "d"), (1.7, "d")])
        sampled = sample_player(fast.grab, sleep=fast.sleep, clock=fast, window=4)
        self.assertTrue(sampled["advanced"])
        self.assertFalse(sampled["audio"])
        self.assertGreaterEqual(sampled["distinct"], 2)
        self.assertEqual(sampled["interval"], 0.45)
        stalled = Clock([(0, "still")])
        stuck = sample_player(stalled.grab, sleep=stalled.sleep, clock=stalled, window=4)
        self.assertTrue(stuck["stalled"])
        self.assertFalse(stuck["advanced"])


class PolicyTests(unittest.TestCase):
    def test_throttle_retries_same_key_and_quota_does_not_switch(self):
        seen = []

        def call(key):
            seen.append(key)
            if key == "a" and seen.count("a") == 1:
                return 429, b'{"error":{"status":"RESOURCE_EXHAUSTED","message":"rate"}}'
            if key == "a":
                body = json.dumps({"candidates": [{"content": {"parts": [{"text": "{\"ok\":true}"}]}}],
                                    "modelVersion": "gemini-3.5-flash-lite",
                                    "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1}}).encode()
                return 200, body
            raise AssertionError("backup")

        result = generate(model="gemini-3.5-flash-lite", parts=[], keys=["a", "b"], call=call, sleep=lambda _s: None)
        self.assertEqual(result["model"], "gemini-3.5-flash-lite")
        self.assertEqual(seen, ["a", "a"])

        def quota(_key):
            return 429, b'{"error":{"message":"GenerateRequestsPerDay quota"}}'

        with self.assertRaises(ProviderFailure) as caught:
            generate(model="gemini-3.5-flash-lite", parts=[], keys=["a", "b"], call=quota, sleep=lambda _s: None)
        self.assertEqual(caught.exception.code, "quota")

    def test_transient_server_error_retries_without_switching_keys(self):
        calls = []

        def unavailable(key):
            calls.append(key)
            return 503, b'{"error":{"status":"UNAVAILABLE"}}'

        with self.assertRaises(ProviderFailure) as caught:
            generate(model="gemini-3.1-flash-lite", parts=[], keys=["first", "backup"],
                     call=unavailable, sleep=lambda _: None)
        self.assertEqual(caught.exception.code, "provider")
        self.assertEqual(calls, ["first", "first", "first"])

    def test_rejected_key_uses_the_backup(self):
        def call(key):
            if key == "old":
                return 403, b'{"error":{"status":"PERMISSION_DENIED"}}'
            body = json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}).encode()
            return 200, body

        generate(model="gemini-3.1-flash-lite", parts=[], keys=["old", "new"], call=call, sleep=lambda _s: None)

    def test_send_confirms_after_reload_and_does_not_repeat(self):
        from shadow.chromium_send import deliver

        thread_url = "https://www.instagram.com/direct/t/101/"
        browser = unittest.mock.Mock()
        browser.page_url.return_value = thread_url
        directory, storage = store()
        self.addCleanup(directory.cleanup)
        self.addCleanup(storage.close)
        conversation = storage.get_or_create_conversation("account", "101")
        decision = storage.add_decision(conversation_id=conversation, source_revision=0,
                                        record_json="{}", versions_json="{}")
        with patch("shadow.chromium_send._controls", side_effect=[
            {"composer": [800, 660], "text": "", "copies": 0}, {"focused": True},
            {"text": "hello", "send": [1210, 660], "documentStarted": 1}, {"copies": 1},
            {"copies": 1, "text": "", "documentStarted": 1},
            {"copies": 1, "text": "", "documentStarted": 2},
        ]):
            args = dict(thread="101", decision=decision, conversation=conversation, revision=0,
                        text="hello", authority="owner_command")
            self.assertEqual(deliver(browser, storage, **args), {"delivery": "confirmed", "sends": 1})
            self.assertEqual(deliver(browser, storage, **args), {"delivery": "prior_attempt", "sends": 0})
        browser.command.assert_any_call("Page.reload", ignoreCache=True)
        self.assertEqual(browser.click.call_count, 2)
        self.assertEqual(storage.outbox_items(conversation)[0]["state"], "confirmed")

    def test_transcript_keeps_duplicates_and_rejects_a_bad_shape(self):
        parsed = parse_transcript({"bubbles": [bubble(1, "left", "haan"), bubble(2, "left", "haan")],
                                   "incomplete": False})
        self.assertEqual(len(parsed["bubbles"]), 2)
        with self.assertRaises(ValueError):
            parse_transcript({"bubbles": [bubble(2, "left", "haan")], "incomplete": False})


class FixtureTests(unittest.TestCase):
    def test_login_viewer_enter_submits_the_focused_form(self):
        from urllib.parse import quote

        from shadow.chromium import chromium, find_chromium
        from tools.chromium_trial import fixture_value, wait_for
        from tools.chromium_viewer import operate

        form = (
            '<form onsubmit="event.preventDefault(); window.submitted = true">'
            '<input type="password" autofocus aria-label="Fixture code"><button>Submit</button></form>'
        )
        with TemporaryDirectory() as directory, chromium(find_chromium(), directory) as browser:
            browser.navigate("data:text/html," + quote(form))
            wait_for(browser, "document.activeElement.type === 'password'", "login fixture ready")
            operate(browser, {"action": "text", "value": "fixture-code"})
            operate(browser, {"action": "key", "value": "Enter"})
            self.assertTrue(fixture_value(browser, "window.submitted === true"))

    def test_fixture_turn_and_coordinate_click(self):
        from shadow.chromium import chromium, find_chromium
        from tools.chromium_trial import fixture_value, local_fixture, wait_for

        hit_page = b"""<!doctype html><style>body{margin:0}button{position:absolute;left:400px;top:200px;width:120px;height:50px}</style>
<button id=hit>Hit</button><script>hit.onclick=()=>window.hit=1</script>"""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(hit_page)))
                self.end_headers()
                self.wfile.write(hit_page)

            def log_message(self, *_args):
                pass

        directory, storage = store()
        self.addCleanup(storage.close)
        self.addCleanup(directory.cleanup)
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, local_fixture() as fixture:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with chromium(find_chromium(), directory.name + "-profile") as browser:
                    browser.navigate(f"http://127.0.0.1:{server.server_port}/")
                    wait_for(browser, "!!document.getElementById('hit')", "hit button")
                    image = open_png(browser.screenshot())
                    self.assertEqual(image.size, (1280, 720))
                    _view, transform = crop_resize(image, (350, 150, 560, 280), (420, 260))
                    browser.click(*transform.to_source(220, 150))
                    wait_for(browser, "window.hit === 1", "mapped click missed the button")

                    browser.navigate(fixture)
                    wait_for(browser, "window.fixtureState === 'inbox'", "inbox")

                    def reorder():
                        fixture_value(browser, "reorder()")

                    stale = act(browser, Vision(), task=TASK_ROW, before_click=reorder)
                    self.assertEqual(stale["reason"], "target_moved")
                    self.assertEqual(fixture_value(browser, "window.fixtureState"), "inbox")
                    reorder()

                    class FixtureVision(Vision):
                        def __init__(self):
                            super().__init__(transcript=[bubble(1, "left", "yaar yeh reel dekha?", "reel")])

                        def choose(self, png, task, choices, candidates):
                            if task == TASK_NAMES:
                                return "unique", 0.97
                            if task == TASK_ROW:
                                return "row-1", 0.97
                            if task == TASK_REEL:
                                return _overlap(candidates, "card", "Open shared reel"), 0.97
                            if task == TASK_CLOSE:
                                return _overlap(candidates, "close", "Close reel"), 0.97
                            return "hold", 0.97

                    def _overlap(candidates, kind, label):
                        raw = fixture_value(browser, (
                            "(() => { const b=[...document.querySelectorAll('button')].find(b => b.innerText.includes("
                            + json.dumps(label) + ")); const r=b.getBoundingClientRect();"
                            " return JSON.stringify([r.left,r.top,r.right,r.bottom]); })()"))
                        left, top, right, bottom = json.loads(raw)
                        hits = [candidate for candidate in candidates if candidate.kind == kind
                                and left <= candidate.center[0] <= right and top <= candidate.center[1] <= bottom]
                        if not hits:
                            raise AssertionError(label)
                        hits.sort(key=lambda candidate: (candidate.box[2] - candidate.box[0]), reverse=True)
                        return hits[0].key

                    def primary(system, user):
                        return reply("haan, dekha"), {"model": "gpt-4o-mini", "usage": {}, "latency_ms": 1}

                    result = run_turn(browser, FixtureVision(), storage, account_id="acct", now=NOW,
                                      primary=primary, fallback=None, track_inbox=True)
                    self.assertEqual(result["outcome"], "draft")
                    self.assertIsNone(claim_inbox_retry(storage, "different-account"))
                    self.assertEqual(claim_inbox_retry(storage, "acct"), "101")
                    pending = {"outcome": "pending"}
                    finish_inbox_turn(storage, "acct", pending)
                    self.assertNotIn("inbox_retry_exhausted", pending)
                    self.assertEqual(claim_inbox_retry(storage, "acct"), "101")
                    finish_inbox_turn(storage, "acct", pending)
                    self.assertTrue(pending["inbox_retry_exhausted"])
                    self.assertIsNone(claim_inbox_retry(storage, "acct"))
                    self.assertEqual(fixture_value(browser, "window.fixtureState"), "Alice")
                    self.assertIn("/direct/t/101/", browser.page_url())
                    self.assertEqual(fixture_value(browser, "document.getElementById('composer').value"), "")
                    self.assertIsNone(fixture_value(browser, "window.fixtureReaction || null"))
                    record = json.loads(storage.decisions_for(result["conversation_id"])[0]["record_json"])
                    self.assertEqual(record["reply"]["messages"], ["haan, dekha"])
                    self.assertFalse(record["media"]["audio"])
                    self.assertIsNone(record["media"]["speech"])
                    self.assertGreaterEqual(record["media"]["distinct"], 2)
                    self.assertEqual(storage.outbox_items(), [])
                    self.assertEqual(result["sends"], 0)
                    self.assertEqual(result["reactions"], 0)

                    fixture_value(browser, "pane.innerHTML = '<h2>Alice</h2><p>Loading messages…</p>'")
                    loading = act(browser, Vision(), task=TASK_ROW)
                    self.assertEqual(loading["outcome"], "wait")
                    self.assertEqual(fixture_value(browser, "window.fixtureState"), "Alice")

                    fixture_value(browser, "chat('Alice')")
                    wait_for(browser, "!!document.getElementById('history')", "thread restored")
                    scrolled = act(browser, Vision(choice="scroll"), task="Scroll the open conversation.")
                    self.assertEqual(scrolled["outcome"], "acted")
                    self.assertGreater(fixture_value(browser, "document.getElementById('history').scrollTop"), 0)
                    self.assertIn(HEADER, (HEADER,))
            finally:
                server.shutdown()
                thread.join()


class OutcomeTests(unittest.TestCase):
    def test_a_post_player_does_not_count_as_a_reel(self):
        from tools.screenshot_turn import live_outcome
        plain = {"row": 4, "bands": 6}
        post = {"player": "p", "advanced": True, "returned": True}
        reel = {"player": "reel", "advanced": True, "returned": True}
        self.assertEqual(live_outcome(plain, post), "dm_opened")
        self.assertEqual(live_outcome(plain, reel), "reel_sampled")
        self.assertEqual(live_outcome(None, None), "session_only")


class OwnerTests(unittest.TestCase):
    def test_owner_takeover_holds(self):
        directory, storage = store()
        self.addCleanup(storage.close)
        self.addCleanup(directory.cleanup)
        conversation = storage.get_or_create_conversation("acct", "101")
        storage.set_owner_active_until(conversation, (NOW + timedelta(hours=1)).isoformat())
        result = draft_and_save(storage, account_id="acct", thread_id="101",
                                bubbles=[bubble(1, "left", "hi")], media=None,
                                primary=lambda *_args: (_ for _ in ()).throw(AssertionError()),
                                fallback=None, now=NOW)
        self.assertEqual(result["reason"], "owner_active")


class HoursTests(unittest.TestCase):
    def test_india_daytime_boundaries(self):
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        self.assertFalse(within_hours(datetime(2026, 11, 22, 5, 59, tzinfo=ist)))
        self.assertTrue(within_hours(datetime(2026, 11, 22, 6, 0, tzinfo=ist)))
        self.assertTrue(within_hours(datetime(2026, 11, 22, 23, 50, tzinfo=ist)))
        self.assertFalse(within_hours(datetime(2026, 11, 22, 0, 0, tzinfo=ist)))
        root = Path(__file__).resolve().parents[1]
        timer = (root / "deploy/shadow-drafts.timer").read_text()
        service = (root / "deploy/shadow-drafts.service").read_text()
        self.assertIn("*-*-* 06..23:00/10:00 Asia/Kolkata", timer)
        self.assertIn("Persistent=false", timer)
        self.assertNotIn("OnUnitInactiveSec", timer)
        self.assertIn("--deliver", service)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.storage = store()
        self.addCleanup(self.storage.close)
        self.addCleanup(self.directory.cleanup)
        self.day = datetime(2026, 11, 22, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    def test_playback_noise_is_the_same_turn_and_a_new_line_is_not(self):
        before = [bubble(1, "left", "yaar yeh reel dekha?", "reel")]
        noisy = [bubble(1, "left", "yaar  yeh reel dekha?"), bubble(2, "right", "Seen")]
        self.assertIsNone(turn_change(before, noisy))
        same = [bubble(1, "right", "", "reel"), bubble(2, "right", "caption")]
        self.assertIsNone(turn_change(same, same))
        self.assertEqual(turn_change(before, before + [bubble(2, "left", "aur ek")]), "stale_turn")
        self.assertEqual(turn_change(before, before + [bubble(2, "right", "maine dekh liya")]), "owner_answered")
        calls = []

        def primary(system, user):
            calls.append(user)
            return reply("dekha"), {"model": "gpt-4o-mini"}

        saved = draft_and_save(self.storage, account_id="acct", thread_id="101", bubbles=before, media={
            "advanced": True, "stalled": False, "audio": False, "speech": None}, primary=primary,
            fallback=None, now=self.day, reread=lambda: noisy)
        self.assertEqual(saved["outcome"], "draft")
        self.assertEqual(len(calls), 1)

    def test_owner_reply_and_reaction_do_not_send(self):
        before = [bubble(1, "left", "hi")]
        owner = draft_and_save(self.storage, account_id="acct", thread_id="101", bubbles=before, media=None,
                               primary=lambda *_: (reply(), {}), fallback=None, now=self.day,
                               reread=lambda: before + [bubble(2, "right", "answered")])
        self.assertEqual(owner["reason"], "owner_answered")
        self.assertEqual(self.storage.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)
        reaction = draft_and_save(self.storage, account_id="acct", thread_id="101",
                                  bubbles=[bubble(1, "left", "You reacted to a message")], media=None,
                                  primary=lambda *_: (_ for _ in ()).throw(AssertionError("model")),
                                  fallback=None, now=self.day)
        self.assertEqual(reaction["reason"], "reaction")

    def test_confirmed_text_is_not_drafted_again(self):
        bubbles = [bubble(1, "left", "hi")]
        first = draft_and_save(self.storage, account_id="acct", thread_id="101", bubbles=bubbles, media=None,
                               primary=lambda *_: (reply("haan"), {"model": "gpt-4o-mini"}), fallback=None, now=self.day)
        item = self.storage.add_outbox_item(decision_id=first["decision_id"], item_index=0,
                                            conversation_id=first["conversation_id"], text="haan",
                                            authority="autonomous", revision=first["revision"])
        self.storage.transition(item, "attempting")
        self.storage.confirm_send(item, {"text": "haan", "evidence": {"reloaded_thread": True}})
        again = draft_and_save(self.storage, account_id="acct", thread_id="101",
                               bubbles=[bubble(1, "left", "hi ")], media=None,
                               primary=lambda *_: (_ for _ in ()).throw(AssertionError("again")),
                               fallback=None, now=self.day)
        self.assertEqual(again["reason"], "already_replied")
        self.assertEqual(again["decision_id"], first["decision_id"])

    def test_first_reply_is_delivered_without_contact_approval(self):
        image = paint(["First message in the open conversation", "Second message in the open conversation",
                       "Third message in the open conversation"], column="thread")
        browser = Shots([image], url="https://www.instagram.com/direct/t/101/")
        sent = []

        def fake_deliver(browser, storage, **kwargs):
            sent.append(kwargs)
            return {"delivery": "confirmed", "sends": 1}

        result = run_turn(browser, Vision(), self.storage, account_id="acct", now=self.day,
                          primary=lambda *_: (reply("haan"), {"model": "gpt-4o-mini"}), fallback=None,
                          deliver=fake_deliver)
        self.assertEqual(result["delivery"], "confirmed")
        self.assertEqual(result["sends"], 1)
        self.assertEqual(sent[0]["text"], "haan")
        self.assertEqual(sent[0]["authority"], "autonomous")
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.storage.get_conversation(result["conversation_id"])["contact_status"],
                         "unknown_pending")

    def test_old_group_blocks_are_removed_but_owner_pause_is_kept(self):
        image = paint(["First message in the open conversation", "Second message in the open conversation",
                       "Third message in the open conversation"], column="thread")

        browser = Shots([image], url="https://www.instagram.com/direct/t/101/")
        conversation = self.storage.get_or_create_conversation("acct", "101")
        self.storage.set_paused_reason(conversation, "group_uncertain")
        sends = []

        def send(_browser, _storage, **kwargs):
            sends.append(kwargs["thread"])
            return {"delivery": "confirmed", "sends": 1}

        def run(**options):
            return run_turn(browser, Vision(), self.storage, account_id="acct", now=self.day,
                            primary=lambda *_: (reply("haan"), {}), fallback=None, deliver=send, **options)

        self.assertEqual(run(allow_threads=["202"])["reason"], "not_authorized")
        self.assertEqual(run()["delivery"], "confirmed")
        self.assertIsNone(self.storage.get_conversation(conversation)["paused_reason"])
        self.storage.set_paused_reason(conversation, "owner_pause")
        self.assertEqual(run()["reason"], "owner_pause")
        self.assertEqual(sends, ["101"])

    def test_outside_hours_does_not_open_the_inbox(self):
        from zoneinfo import ZoneInfo
        night = datetime(2026, 11, 22, 5, 59, tzinfo=ZoneInfo("Asia/Kolkata"))

        class Dead:
            def navigate(self, url):
                raise AssertionError(url)

        result = run_check(Dead(), Vision(), self.storage, account_id="acct", now=night,
                           primary=lambda *_: (_ for _ in ()).throw(AssertionError("model")),
                           fallback=None, deliver=lambda **_: (_ for _ in ()).throw(AssertionError("send")))
        self.assertEqual(result["outcome"], "outside_hours")
        self.assertEqual(result["sends"], 0)

    def test_scheduled_check_waits_for_first_usable_inbox_capture(self):
        inbox = paint(["Alice Example", "Bobby Example"])
        blank = Image.new("RGB", inbox.size, "white")

        class LoadingInbox(Shots):
            def navigate(self, url):
                self.url = url

            def screenshot(self):
                if self.n == 0:
                    self.n += 1
                    raise BrowserError("Chromium refused Page.captureScreenshot.")
                return super().screenshot()

        browser = LoadingInbox([blank, blank, inbox])
        result = run_check(browser, Vision(choice="hold"), self.storage, account_id="acct", now=self.day,
                           primary=lambda *_: self.fail("No unread chat"), fallback=None,
                           deliver=lambda *_args, **_kwargs: self.fail("No reply"), sleep=lambda _: None)

        self.assertEqual([step["reason"] for step in result["results"]], ["hold"])
        self.assertEqual(result["results"][0]["screen"], "inbox")
        self.assertEqual(browser.clicks, [])

    def test_queue_survives_a_cleared_badge(self):
        note_opened(self.storage, "acct", "101")
        self.assertEqual(claim_next(self.storage, "acct"), "101")
        self.assertIsNone(claim_next(self.storage, "acct", skip={"101"}))
        self.assertEqual(self.storage.get_work(self.storage.get_or_create_conversation("acct", "101")), None)

    def test_uncertain_restart_confirms_or_holds_without_a_second_click(self):
        from shadow.chromium_send import deliver, reconcile
        conversation = self.storage.get_or_create_conversation("acct", "101")
        decision = self.storage.add_decision(conversation_id=conversation, source_revision=1,
                                             record_json="{}", versions_json="{}")
        item = self.storage.add_outbox_item(decision_id=decision, item_index=0, conversation_id=conversation,
                                            text="hello", authority="autonomous", revision=1)
        self.storage.transition(item, "attempting")
        self.assertEqual(self.storage.recover_after_restart(), 1)
        browser = unittest.mock.Mock()
        browser.page_url.return_value = "https://www.instagram.com/direct/t/101/"
        with patch("shadow.chromium_send.sleep", lambda _seconds: None), patch(
                "shadow.chromium_send._controls", side_effect=[
                    {"documentStarted": 1},
                    {"copies": 0, "text": "", "documentStarted": 2},
                ]):
            self.assertEqual(reconcile(browser, self.storage, self.storage.get_outbox_item(item), "101"), "held")
        self.assertEqual(self.storage.get_outbox_item(item)["state"], "uncertain")
        blocked = deliver(browser, self.storage, thread="101", decision=decision, conversation=conversation,
                          revision=1, text="hello", authority="autonomous")
        self.assertEqual(blocked, {"delivery": "prior_attempt", "sends": 0})
        browser.click.assert_not_called()
        with patch("shadow.chromium_send.sleep", lambda _seconds: None), patch(
                "shadow.chromium_send._controls", side_effect=[
                    {"documentStarted": 3},
                    {"copies": 1, "text": "", "documentStarted": 4},
                ]):
            self.assertEqual(reconcile(browser, self.storage, self.storage.get_outbox_item(item), "101"), "confirmed")
        self.assertEqual(self.storage.get_outbox_item(item)["state"], "confirmed")
        browser.click.assert_not_called()

    def test_hours_close_after_typing_does_not_submit(self):
        from shadow.chromium_send import deliver
        conversation = self.storage.get_or_create_conversation("acct", "101")
        decision = self.storage.add_decision(conversation_id=conversation, source_revision=1,
                                             record_json="{}", versions_json="{}")
        browser = unittest.mock.Mock()
        browser.page_url.return_value = "https://www.instagram.com/direct/t/101/"
        calls = {"n": 0}

        def allow():
            calls["n"] += 1
            return calls["n"] == 1

        with patch("shadow.chromium_send._controls", side_effect=[
            {"composer": [800, 660], "text": "", "copies": 0},
            {"focused": True},
            {"text": "hello", "send": [1210, 660], "documentStarted": 1},
        ]):
            result = deliver(browser, self.storage, thread="101", decision=decision, conversation=conversation,
                             revision=1, text="hello", authority="autonomous", allow=allow)
        self.assertEqual(result, {"delivery": "outside_hours", "sends": 0})
        self.assertEqual(self.storage.outbox_items(), [])
        self.assertEqual(browser.click.call_count, 1)


if __name__ == "__main__":
    unittest.main()

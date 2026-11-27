"""Reply lab: identical frozen inputs, per-model reviews, no live delivery."""

import inspect
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from shadow.context_memory import STYLE_RULES
from shadow.decisions import ModelResponse
from shadow.gemini_client import ProviderFailure
from shadow.model_openai import ProviderError
from shadow.reply_lab import (
    HISTORICAL_CASES, Lab, classify_reply, default_fake_lineup, freeze_case, gemini_reply,
    kiro_reply, load_archive, make_handler, normalize_lineup, openai_reply, bedrock_reply,
    prepared_lineup, reply_system, sarvam_reply, tally, _bubbles, _reply_json,
)
from shadow.screenshot_loop import voice_text
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


VALID = _reply_json(["ha"])
ROOT = Path(__file__).resolve().parents[1]


class ReplyLabTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.lab = Lab(self.tmp.name, trace=True, timeout=2)
        self.lab.set_lineup(default_fake_lineup())

    def tearDown(self):
        self.lab.close()
        self.tmp.cleanup()

    def test_contract_statuses_stay_distinct(self):
        self.assertEqual(classify_reply(VALID)["status"], "reply")
        self.assertEqual(classify_reply(_reply_json([], "skip", "nothing_open"))["status"], "skip")
        self.assertEqual(classify_reply(_reply_json([], "hold", "hold"))["status"], "hold")
        self.assertEqual(classify_reply("nope")["status"], "invalid")
        self.assertEqual(classify_reply(_reply_json(["one", "two"]))["status"], "invalid")

    def test_freeze_uses_the_screenshot_prompt_once(self):
        case = self.lab.case("priya-talk")
        frozen = freeze_case(case)
        self.assertEqual(frozen["system"], reply_system(extra=tuple(case["rules"])))
        self.assertIn(STYLE_RULES["tone"]["serious"], frozen["system"])
        self.assertEqual(frozen["system"].count(voice_text()), 1)
        self.assertEqual(frozen["user"], json.dumps(
            {"bubbles": case["bubbles"], "media": case["media"]}, ensure_ascii=False))
        self.assertNotIn("Priya", frozen["system"] + frozen["user"])
        self.assertNotIn("silence", frozen["user"])

    def test_reference_in_the_voice_guide_is_not_sent(self):
        reference = "the lighthouse is blue"
        case_id = self.lab.add_case(
            title="Leaked example", contact="Maya", language="en", tone="casual",
            bubbles=_bubbles(("left", "did the file arrive?")),
            reference=reference, authorship="synthetic")
        before = len(self.lab.calls())
        with patch("shadow.screenshot_loop.voice_text", return_value=f"Example: {reference}"):
            with self.assertRaisesRegex(ValueError, "reference reply appears in the prompt"):
                self.lab.generate(case_id)
            self.assertIn("stays out of scored runs", self.lab.case(case_id)["blocked"])
        self.assertEqual(len(self.lab.calls()), before)

    def test_reference_inside_the_transcript_is_not_sent(self):
        case_id = self.lab.add_case(
            title="Leaked turn", contact="Maya", language="en", tone="casual",
            bubbles=_bubbles(("left", "the note is zebra-token-991")),
            reference="zebra-token-991", authorship="owner_written")
        with self.assertRaises(ValueError):
            self.lab.generate(case_id)
        self.assertEqual(self.lab.calls(), [])

    def test_generate_does_not_read_live_memory_or_production_state(self):
        source = inspect.getsource(inspect.getmodule(Lab))
        for name in ("add_outbox_item", "enqueue_owner_send", "add_decision",
                     "open_storage", "draft_and_save", "send_one", "LiveVision"):
            self.assertNotIn(name, source)
        with patch("shadow.context_memory.ContextMemory.rules", return_value=()) as rules, \
                patch("shadow.storage.Storage.add_decision", side_effect=AssertionError("decision")), \
                patch("shadow.storage.Storage.add_outbox_item", side_effect=AssertionError("outbox")), \
                patch("shadow.outbox.enqueue_owner_send", side_effect=AssertionError("send")), \
                patch("shadow.reply_lab.OpenAITransport", side_effect=AssertionError("openai")), \
                patch("shadow.reply_lab.gemini_generate", side_effect=AssertionError("gemini")), \
                patch("shadow.reply_lab.BedrockTransport", side_effect=AssertionError("bedrock")):
            run_id = self.lab.generate("dev-close")
        self.assertFalse(rules.called)
        self.assertEqual(len(self.lab.run(run_id)["candidates"]), 16)
        self.assertTrue(self.lab.run(run_id)["complete"])

    def test_owner_answered_turn_is_not_drafted(self):
        case_id = self.lab.add_case(
            title="Already sent", contact="Dev", language="en", tone="casual",
            bubbles=_bubbles(("left", "ok cool"), ("right", "yea")))
        with self.assertRaises(ValueError) as caught:
            self.lab.generate(case_id)
        self.assertIn("owner_answered", str(caught.exception))
        self.assertEqual(self.lab.calls(), [])

    def test_retry_keeps_successful_candidates_and_the_original_input(self):
        self.lab.set_lineup([
            {"provider": "fake", "model": "lab-alpha", "fail_once": [3],
             "script": [f"amber {index}" for index in range(1, 5)]},
            {"provider": "fake", "model": "lab-bravo", "invalid_once": [1],
             "script": [f"birch {index}" for index in range(1, 5)]},
            {"provider": "fake", "model": "lab-charlie",
             "script": [f"cedar {index}" for index in range(1, 5)]},
            {"provider": "fake", "model": "lab-delta",
             "script": [f"dune {index}" for index in range(1, 5)]},
        ])
        run_id = self.lab.generate("maya-file")
        before = {(item["slot"], item["sample"]): (item["status"], item["messages"], item["attempts"])
                  for item in self.lab.run(run_id)["candidates"]}
        self.assertEqual(before[(0, 3)][0], "failure")
        self.assertEqual(before[(1, 1)][0], "invalid")
        self.assertEqual(before[(2, 0)][0], "reply")
        frozen_user = self.lab.run(run_id)["user"]
        self.lab.case("maya-file")  # later edits must not change a retry
        self.lab.retry(run_id)
        after = self.lab.run(run_id)
        found = {(item["slot"], item["sample"]): item for item in after["candidates"]}
        self.assertEqual(found[(0, 3)]["status"], "reply")
        self.assertEqual(found[(0, 3)]["attempts"], 2)
        self.assertEqual(found[(0, 3)]["prior"][0]["status"], "failure")
        self.assertEqual(found[(1, 1)]["status"], "reply")
        self.assertEqual(found[(1, 1)]["attempts"], 2)
        self.assertEqual(tuple(found[(2, 0)]["messages"]), tuple(before[(2, 0)][1]))
        self.assertEqual(found[(2, 0)]["attempts"], 1)
        self.assertNotIn("yea got it, check downloads", after["system"] + after["user"])
        retried = [item for item in self.lab.calls() if item["attempt"] == 2]
        self.assertEqual(len(retried), 2)
        self.assertTrue(all(item["user"] == frozen_user for item in retried))

    def test_timeout_does_not_replace_a_finished_slot(self):
        self.lab.timeout = 0.05
        self.lab.set_lineup([
            {"provider": "fake", "model": "lab-alpha", "sleep": 0.4, "sleep_samples": [0],
             "script": [f"amber {index}" for index in range(1, 5)]},
            {"provider": "fake", "model": "lab-bravo", "script": [f"birch {index}" for index in range(1, 5)]},
            {"provider": "fake", "model": "lab-charlie", "script": [f"cedar {index}" for index in range(1, 5)]},
            {"provider": "fake", "model": "lab-delta", "script": [f"dune {index}" for index in range(1, 5)]},
        ])
        run_id = self.lab.generate("neel-demo")
        found = {(item["slot"], item["sample"]): item for item in self.lab.run(run_id)["candidates"]}
        self.assertEqual(found[(0, 0)]["status"], "failure")
        self.assertEqual(found[(0, 0)]["error_code"], "timeout")
        self.assertEqual(found[(0, 1)]["messages"], ["amber 2"])
        self.assertEqual(found[(1, 0)]["messages"], ["birch 1"])

    def test_tie_is_not_awarded_by_display_order_and_attempt_one_stays_unrated(self):
        shared = ["same reply", "second", "third", "fourth"]
        self.lab.set_lineup([
            {"provider": "fake", "model": "model-a", "script": shared},
            {"provider": "fake", "model": "model-b", "script": shared},
            {"provider": "fake", "model": "model-c", "script": [f"cee {index}" for index in range(1, 5)]},
            {"provider": "fake", "model": "model-d", "script": [f"dee {index}" for index in range(1, 5)]},
        ])
        run_id = self.lab.generate("aisha-mix")
        run = self.lab.run(run_id)
        display = next(index for index, (slot, sample) in enumerate(run["shuffle"], 1)
                       if (slot, sample) == (1, 0))
        self.lab.vote(run_id, f"d{display}", [], False, "")
        report = self.lab.results()
        by_model = {row["model"]: row for row in report["models"]}
        self.assertEqual(report["ranked_turns"], 1)
        self.assertEqual(report["ties"], 1)
        self.assertEqual(by_model["model-a"]["sole_wins"], 0)
        self.assertEqual(by_model["model-b"]["sole_wins"], 0)
        self.assertEqual(by_model["model-a"]["tie_credits"], 1)
        self.assertEqual(by_model["model-b"]["tie_credits"], 1)
        self.assertEqual((by_model["model-b"]["sendable"], by_model["model-b"]["judged"]), (1, 1))
        self.assertEqual((by_model["model-a"]["sendable"], by_model["model-a"]["judged"], by_model["model-a"]["unrated"]),
                         (0, 0, 1))
        self.assertEqual(by_model["model-c"]["unrated"], 1)

    def test_optional_ratings_and_turn_outcomes(self):
        self.lab.set_lineup(default_fake_lineup())
        first = self.lab.generate("rohit-plan")
        run = self.lab.run(first)
        picked = next(index for index, (slot, sample) in enumerate(run["shuffle"], 1) if (slot, sample) == (2, 1))
        marked = next(index for index, (slot, sample) in enumerate(run["shuffle"], 1) if (slot, sample) == (0, 0))
        self.lab.vote(first, f"d{picked}", [marked], True, "")
        second = self.lab.generate("dev-close")
        self.lab.vote(second, "no_reply", [], False, "")
        third = self.lab.generate("neel-demo")
        self.lab.vote(third, "none", [], False, "I would have asked which slide")
        report = self.lab.results()
        by_model = {row["model"]: row for row in report["models"]}
        self.assertEqual(report["ranked_turns"], 3)
        self.assertEqual(report["no_reply"], 1)
        self.assertEqual(report["none"], 1)
        self.assertEqual((by_model["lab-alpha"]["sendable"], by_model["lab-alpha"]["judged"]), (1, 1))
        self.assertEqual((by_model["lab-charlie"]["sendable"], by_model["lab-charlie"]["judged"]), (0, 1))
        pending = self.lab.generate("aisha-mix")
        with self.lab._lock:
            self.lab.db.execute(
                "UPDATE candidates SET status = 'pending' WHERE run_id = ? AND slot = 0 AND sample = 0",
                (pending,))
            self.lab.db.commit()
        self.assertEqual(self.lab.results()["ranked_turns"], 3)
        self.assertEqual(self.lab.results()["incomplete"][0]["id"], pending)

    def test_continuation_keeps_only_the_reply_you_chose(self):
        self.lab.set_lineup(default_fake_lineup())
        run_id = self.lab.generate("maya-file")
        run = self.lab.run(run_id)
        display = next(index for index, (slot, sample) in enumerate(run["shuffle"], 1) if (slot, sample) == (0, 0))
        self.lab.vote(run_id, f"d{display}", [], False, "my actual reply")
        case_id = self.lab.continue_run(run_id, "what time thursday\n")
        case = self.lab.case(case_id)
        texts = [(item["side"], item["text"]) for item in case["bubbles"]]
        self.assertEqual(texts, [
            ("left", "did the file arrive?"),
            ("right", "my actual reply"),
            ("left", "what time thursday"),
        ])
        self.assertFalse(case["reference"])
        self.lab.trace_log.clear()
        self.lab.generate(case_id)
        users = {item["user"] for item in self.lab.calls()}
        systems = {item["system"] for item in self.lab.calls()}
        self.assertEqual(systems, {reply_system(extra=())})
        self.assertEqual(len(users), 1)
        user = users.pop()
        self.assertIn("my actual reply", user)
        self.assertNotIn("amber 1", user)
        self.assertNotIn("amber 2", user)
        self.assertNotIn("birch 1", user)
        self.assertNotIn("yea got it, check downloads", user)
        none_id = self.lab.generate("dev-close")
        self.lab.vote(none_id, "none", [], False, "")
        with self.assertRaises(ValueError):
            self.lab.continue_run(none_id, "still here")

    def test_storage_cannot_sit_inside_the_checkout(self):
        with self.assertRaises(ValueError):
            Lab(ROOT)

    def test_adapters_share_one_input_and_do_not_substitute_a_model(self):
        spec = {"provider": "openai", "model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1", "region": ""}
        seen = {}

        class Transport:
            def __init__(self, **kwargs):
                seen["init"] = kwargs

            async def generate(self, role, system, text):
                seen["call"] = (role, system, text)
                return ModelResponse(VALID, {}, "gpt-4o-mini", "end_turn")

        with patch("shadow.reply_lab.openai_api_key", return_value="test-key"), \
                patch("shadow.reply_lab.OpenAITransport", Transport), \
                patch("shadow.reply_lab.gemini_generate") as gemini:
            self.assertEqual(openai_reply(spec, "system-text", "user-text"), VALID)
        gemini.assert_not_called()
        self.assertEqual(seen["call"], ("reply", "system-text", "user-text"))
        self.assertEqual(seen["init"]["temperature"], 0.7)
        self.assertEqual(seen["init"]["max_tokens"], 200)
        self.assertTrue(seen["init"]["json_object"])
        self.assertEqual(seen["init"]["models"]["reply"], "gpt-4o-mini")

        class Boom(Transport):
            async def generate(self, role, system, text):
                raise ProviderError("provider_rate_limited")

        with patch("shadow.reply_lab.openai_api_key", return_value="test-key"), \
                patch("shadow.reply_lab.OpenAITransport", Boom), \
                patch("shadow.reply_lab.gemini_generate") as gemini:
            with self.assertRaises(ProviderFailure) as caught:
                openai_reply(spec, "system-text", "user-text")
        self.assertEqual(caught.exception.code, "rate_limited")
        gemini.assert_not_called()

        def fake_gemini(**kwargs):
            seen["gemini"] = kwargs
            return {"text": VALID, "model": kwargs["model"], "usage": {}}

        with patch("shadow.reply_lab.gemini_keys", return_value=["key"]), \
                patch("shadow.reply_lab.gemini_generate", fake_gemini):
            self.assertEqual(gemini_reply(
                {"provider": "gemini", "model": "gemini-test"}, "system-text", "user-text"), VALID)
        self.assertEqual(seen["gemini"]["parts"], [{"text": "user-text"}])
        self.assertEqual(seen["gemini"]["system"], "system-text")
        self.assertEqual(seen["gemini"]["temperature"], 0.7)
        self.assertEqual(seen["gemini"]["max_output_tokens"], 200)
        self.assertEqual(seen["gemini"]["attempts"], 2)
        self.assertEqual(seen["gemini"]["timeout"], 20)
        self.assertIsNone(seen["gemini"]["thinking"])

        class Bedrock:
            def __init__(self, **kwargs):
                seen["bedrock"] = kwargs

            async def generate(self, role, system, text):
                seen["bedrock_call"] = (role, system, text)
                return ModelResponse(VALID, {}, "bedrock-model", "end_turn")

        with patch("shadow.reply_lab.BedrockTransport", Bedrock):
            self.assertEqual(bedrock_reply(
                {"provider": "bedrock", "model": "bedrock-model", "region": "us-east-1"},
                "system-text", "user-text"), VALID)
        self.assertEqual(seen["bedrock_call"], ("reply", "system-text", "user-text"))
        self.assertEqual(seen["bedrock"]["inference"]["reply"]["temperature"], 0.7)
        self.assertEqual(seen["bedrock"]["inference"]["reply"]["maxTokens"], 200)

    def test_sarvam_sends_null_reasoning_effort_with_its_own_key(self):
        seen = {}

        class Transport:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            async def generate(self, role, system, text):
                return ModelResponse(VALID, {}, "sarvam-105b", "end_turn")

        with patch.dict(os.environ, {"SARVAM_API_KEY": "sarvam-test-key"}), \
                patch("shadow.reply_lab.OpenAITransport", Transport):
            self.assertEqual(sarvam_reply(
                {"provider": "sarvam", "model": "sarvam-105b", "base_url": "https://api.sarvam.ai/v1"},
                "system-text", "user-text"), VALID)
        self.assertEqual(seen["api_key"], "sarvam-test-key")
        self.assertEqual(seen["body_extra"], {"reasoning_effort": None})
        self.assertEqual(seen["base_url"], "https://api.sarvam.ai/v1")
        self.assertTrue(seen["json_object"])
        self.assertEqual(seen["temperature"], 0.7)

    def test_kiro_key_is_not_sent_off_loopback(self):
        with patch.dict(os.environ, {"REPLY_LAB_KIRO_API_KEY": "kiro-test-key"}), \
                patch("shadow.reply_lab.build_opener", side_effect=AssertionError("sent")):
            with self.assertRaises(ProviderFailure):
                kiro_reply(
                    {"provider": "kiro", "model": "claude-sonnet-5.5", "base_url": "https://example.com/v1"},
                    "system-text", "user-text")

        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                seen["path"] = self.path
                seen["key"] = self.headers.get("x-api-key")
                seen["body"] = json.loads(self.rfile.read(length))
                payload = json.dumps({
                    "content": [{"type": "text", "text": VALID}], "stop_reason": "end_turn",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with patch.dict(os.environ, {"REPLY_LAB_KIRO_API_KEY": "kiro-test-key"}):
            text = kiro_reply(
                {"provider": "kiro", "model": "claude-sonnet-5.5", "base_url": f"http://127.0.0.1:{port}/v1"},
                "system-text", "user-text")
        self.assertEqual(text, VALID)
        self.assertEqual(seen["path"], "/v1/messages")
        self.assertEqual(seen["key"], "kiro-test-key")
        self.assertEqual(seen["body"]["model"], "claude-sonnet-5.5")
        self.assertEqual(seen["body"]["max_tokens"], 200)
        self.assertEqual(seen["body"]["temperature"], 0.7)
        self.assertEqual(server.server_address[0], "127.0.0.1")

    def test_prepared_lineup_keeps_gemini_slots_distinct(self):
        with patch.dict(os.environ, {
            "REPLY_LAB_SARVAM_MODEL": "sarvam-105b",
            "REPLY_LAB_SARVAM_BASE_URL": "https://api.sarvam.ai/v1",
            "REPLY_LAB_GEMINI_FIRST_MODEL": "gemini-3.5-flash-lite",
            "REPLY_LAB_GEMINI_SECOND_MODEL": "gemini-3.1-flash-lite",
            "REPLY_LAB_KIRO_MODEL": "claude-sonnet-5.5",
            "REPLY_LAB_KIRO_BASE_URL": "http://127.0.0.1:8000/v1",
        }):
            normal = normalize_lineup(prepared_lineup())
        self.assertEqual([item["provider"] for item in normal], ["sarvam", "gemini", "gemini", "kiro"])
        self.assertEqual([item["model"] for item in normal], [
            "sarvam-105b", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "claude-sonnet-5.5"])
        self.assertNotIn("api_key", json.dumps(normal))

    def test_checked_short_reference_stays_out_of_the_prompt(self):
        case_id = self.lab.add_case(
            title="Short", contact="Direct", language="archived", tone="casual",
            bubbles=_bubbles(("right", "ping"), ("left", "did the parcel land")),
            reference="Ha", authorship="owner_sent", checked=True)
        case = self.lab.case(case_id)
        frozen = freeze_case(case)
        self.assertIsNone(case["blocked"])
        self.assertNotIn("Ha", frozen["user"])
        self.lab.generate(case_id)
        self.assertEqual(len(self.lab.calls()), 16)
        self.assertNotIn("Ha", self.lab.calls()[0]["user"])

    def test_one_failed_slot_retries_without_replacing_the_other(self):
        slots = []
        for index, name in enumerate(("lab-alpha", "lab-bravo", "lab-charlie", "lab-delta")):
            slots.append({
                "provider": "fake", "model": name,
                "script": [f"{name} {number}" for number in range(1, 5)],
                "fail_once": [0] if index < 2 else [],
            })
        self.lab.set_lineup(slots)
        run_id = self.lab.generate("maya-file")
        run = self.lab.run(run_id)
        failed = [display for display, pair in enumerate(run["shuffle"], 1)
                  if run["candidates"] and any(item["slot"] == pair[0] and item["sample"] == pair[1]
                                               and item["status"] == "failure" for item in run["candidates"])]
        self.assertEqual(len(failed), 2)
        self.lab.retry(run_id, failed[0])
        again = self.lab.run(run_id)
        found = {(item["slot"], item["sample"]): item for item in again["candidates"]}
        first = tuple(run["shuffle"][failed[0] - 1])
        second = tuple(run["shuffle"][failed[1] - 1])
        self.assertEqual(found[first]["status"], "reply")
        self.assertEqual(found[first]["attempts"], 2)
        self.assertEqual(found[second]["status"], "failure")
        self.assertEqual(found[second]["attempts"], 1)

    def test_dashboard_groups_named_models_and_persists_choices(self):
        words = ("pebble", "quartz", "flint", "slate")
        models = ("secret-model-quartz", "secret-model-pebble", "secret-model-flint", "secret-model-slate")
        slots = []
        for index, (word, model) in enumerate(zip(words, models)):
            slots.append({
                "provider": "fake", "model": model,
                "script": [f"{word} {number}" for number in range(1, 5)],
                "fail_once": [3] if index == 2 else [],
            })
        self.lab.set_lineup(slots)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.lab))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertEqual(server.server_address[0], "127.0.0.1")

        def open_page(path, data=None):
            payload = None if data is None else urlencode(data, doseq=True).encode()
            request = Request(base + path, data=payload, method="POST" if data is not None else "GET")
            with urlopen(request, timeout=10) as response:
                return response.read().decode(), urlsplit(response.geturl()).path

        with patch("shadow.reply_lab.OpenAITransport", side_effect=AssertionError("live openai")), \
                patch("shadow.reply_lab.gemini_generate", side_effect=AssertionError("live gemini")), \
                patch("shadow.reply_lab.BedrockTransport", side_effect=AssertionError("live bedrock")):
            page, path = open_page("/runs", {"case_id": "maya-file"})
        run_id = path.rsplit("/", 1)[-1]
        again, _ = open_page(path)
        self.assertEqual(page, again)
        for slot, model in enumerate(models):
            panel = page.split(f"data-testid='model-panel-{slot}'", 1)[1].split("</section>", 1)[0]
            self.assertIn(model, panel)
            self.assertEqual(panel.count("data-testid='draft-"), 4)
            self.assertTrue(all(other not in panel for other in models if other != model))
            self.assertIn(f"{words[slot]} 1", panel)
        self.assertNotIn("yea got it, check downloads", page)
        self.assertIn("did the file arrive?", page)
        self.assertIn("Latest inbound", page)
        self.assertIn(">Failed<", page)
        calls = self.lab.calls()
        self.assertEqual(len(calls), 16)
        self.assertEqual({(item["system"], item["user"]) for item in calls}, {
            (self.lab.run(run_id)["system"], self.lab.run(run_id)["user"])})
        self.assertEqual(self.lab.run(run_id)["system"], reply_system())
        self.assertNotIn("yea got it, check downloads", calls[0]["system"] + calls[0]["user"])
        self.assertEqual(sorted(item["model"] for item in calls), sorted(model for model in models for _ in range(4)))
        with self.assertRaises(HTTPError) as rejected:
            open_page(path + "/pick", {"slot": 2, "sample": 3})
        self.assertEqual(rejected.exception.code, 400)
        self.assertEqual(self.lab.run(run_id)["model_picks"], {})
        kept = {(item["slot"], item["sample"]): item["messages"]
                for item in self.lab.run(run_id)["candidates"] if item["status"] == "reply"}
        open_page(f"/runs/{run_id}/retry", {"retry": "1"})
        retried = self.lab.run(run_id)
        found = {(item["slot"], item["sample"]): item for item in retried["candidates"]}
        self.assertEqual(found[(2, 3)]["status"], "reply")
        self.assertEqual(found[(2, 3)]["attempts"], 2)
        self.assertEqual(found[(2, 3)]["messages"], ["flint 4"])
        for key, messages in kept.items():
            self.assertEqual(found[key]["messages"], messages)
            self.assertEqual(found[key]["attempts"], 1)
        page, _ = open_page(path)
        self.assertIn("flint 4", page)
        for slot, sample in enumerate((0, 1, 3, None)):
            option = "none" if sample is None else str(sample)
            reviewed, _ = open_page(path + "/pick", {"slot": slot, "sample": option})
            self.assertIn(f"{slot + 1} of 4 models reviewed", reviewed)
            if slot < 3:
                self.assertNotIn("yea got it, check downloads", reviewed)
                refreshed, _ = open_page(path)
                self.assertIn(f"{slot + 1} of 4 models reviewed", refreshed)
        self.assertIn("yea got it, check downloads", reviewed)
        reopened = Lab(self.tmp.name)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.run(run_id)["model_picks"], {0: 0, 1: 1, 2: 3, 3: None})
        report = reopened.results()
        self.assertEqual(report["reviewed_cases"], 1)
        by_model = {row["model"]: row for row in report["models"]}
        self.assertEqual(by_model[models[2]]["drafts_picked"], 1)
        self.assertEqual(by_model[models[3]]["none_fit"], 1)
        home, _ = open_page("/board")
        self.assertIn("1 case reviewed", home)
        self.assertIn(models[0], home)

    def test_archive_import_click_advances_and_refresh_resumes(self):
        path = Path(self.tmp.name) / "cases.json"
        path.write_text(json.dumps(_mini_archive()))
        self.assertEqual(self.lab.import_archive(path), 3)
        self.assertEqual(self.lab.import_archive(path), 0)
        historical = [case for case in self.lab.cases() if case["seq"] is not None]
        self.assertEqual([case["rules"] for case in historical], [[], [], []])
        self.assertEqual([case["reference"] for case in historical], ["Ha", "ok", "yo"])
        self.assertTrue(all(case["checked"] and not case["blocked"] for case in historical))
        for case in historical:
            self.assertNotIn(case["reference"], freeze_case(case)["user"])
        models = ("secret-model-one", "secret-model-two", "secret-model-three", "secret-model-four")
        self.lab.set_lineup([
            {"provider": "fake", "model": model, "script": [f"{model[-3:]} {number}" for number in range(1, 5)]}
            for model in models])
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.lab))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def open_page(path, data=None):
            payload = None if data is None else urlencode(data).encode()
            request = Request(base + path, data=payload, method="POST" if data is not None else "GET")
            with urlopen(request, timeout=10) as response:
                return response.read().decode(), urlsplit(response.geturl()).path

        home, home_path = open_page("/")
        self.assertEqual(home_path, "/cases/ig-0000000000000001")
        self.assertIn("Start", home)
        self.assertIn("Case 1 of 3", home)
        self.assertNotIn(">Ha<", home)
        page, path = open_page("/runs", {"case_id": "ig-0000000000000001"})
        run_id = path.rsplit("/", 1)[-1]
        first = self.lab.run(run_id)
        first_calls = [item for item in self.lab.calls() if item["user"] == first["user"]]
        self.assertEqual(len(first_calls), 16)
        self.assertEqual({(item["system"], item["user"]) for item in first_calls}, {(first["system"], first["user"])})
        for model in models:
            self.assertIn(model, page)
        self.assertNotIn(">Ha<", page)
        self.assertIn("None fit", page)
        deadline = time.monotonic() + 5
        while self.lab.latest_run_id("ig-0000000000000002") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(self.lab.latest_run_id("ig-0000000000000002"))
        self.assertIsNone(self.lab.latest_run_id("ig-0000000000000003"))

        for slot, sample in enumerate(("0", "1", "none", "3")):
            _body, landed = open_page(path + "/pick", {"slot": slot, "sample": sample})
            if slot < 3:
                self.assertEqual(landed, path)
                self.assertEqual(open_page("/")[1], path)
        self.assertEqual(self.lab.run(run_id)["model_picks"], {0: 0, 1: 1, 2: None, 3: 3})
        self.assertEqual(landed, f"/runs/{self.lab.latest_run_id('ig-0000000000000002')}")
        deadline = time.monotonic() + 5
        while self.lab.latest_run_id("ig-0000000000000003") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(self.lab.latest_run_id("ig-0000000000000003"))
        resumed, resumed_path = open_page("/")
        self.assertEqual(resumed_path, landed)
        self.assertIn("0 of 4 models reviewed", resumed)
        for model in models:
            self.assertIn(model, resumed)
        reopened = Lab(self.tmp.name)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.resume_target(), landed)
        open_page(landed + "/pick", {"slot": 0, "sample": "0"})
        open_page(landed + "/pick", {"slot": 0, "sample": "2"})
        self.assertEqual(reopened.run(self.lab.latest_run_id("ig-0000000000000002"))["model_picks"], {0: 2})
        if self.lab._ahead_thread is not None:
            self.lab._ahead_thread.join(timeout=5)

    def test_archive_rejects_a_reference_at_or_before_the_cutoff(self):
        payload = _mini_archive()
        payload["references_by_case_id"]["ig-0000000000000001"]["messages"][0]["sent_at"] = "2026-01-01T00:00:01+00:00"
        path = Path(self.tmp.name) / "early.json"
        path.write_text(json.dumps(payload))
        with self.assertRaises(ValueError) as caught:
            load_archive(path)
        self.assertIn("cutoff", str(caught.exception))

    def test_real_archive_imports_when_present(self):
        if not HISTORICAL_CASES.is_file():
            self.skipTest("historical archive is local")
        raw = json.loads(HISTORICAL_CASES.read_text())
        self.assertEqual(self.lab.import_archive(HISTORICAL_CASES), 160)
        historical = sorted((case for case in self.lab.cases() if case["seq"] is not None), key=lambda case: case["seq"])
        self.assertEqual(len(historical), 160)
        self.assertEqual(self.lab.resume_target(), f"/cases/{raw['cases'][0]['case_id']}")
        short = 0
        for case, raw_case in zip(historical, raw["cases"]):
            messages = raw_case["model_input"]["prior_messages"] + raw_case["model_input"]["inbound_messages"]
            self.assertEqual(len(case["bubbles"]), len(messages))
            self.assertEqual(case["rules"], [])
            self.assertTrue(case["checked"])
            self.assertFalse(case["blocked"])
            self.assertEqual(case["authorship"], "owner_sent")
            if len(case["reference"]) < 8:
                short += 1
        self.assertGreater(short, 0)


def _mini_archive():
    def item(case_id, inbound, reference, ref_at):
        return case_id, {
            "case_id": case_id,
            "category": "plans_and_logistics",
            "conversation_type": "direct",
            "cutoff_at": "2026-01-01T00:00:01+00:00",
            "source": {"chat_log": "local/chat_log.jsonl", "first_line": 1, "last_inbound_line": 2},
            "model_input": {
                "prior_messages": [{
                    "id": 1, "sent_at": "2026-01-01T00:00:00+00:00", "role": "owner", "text": "ping",
                }],
                "inbound_messages": [{
                    "id": 2, "sent_at": "2026-01-01T00:00:01+00:00", "role": "contact", "text": inbound,
                }],
            },
        }, {
            "provenance": "owner_account_sent_human_authorship_unverified",
            "messages": [{"id": 3, "sent_at": ref_at, "role": "owner", "text": reference}],
        }

    rows = [
        item("ig-0000000000000001", "did the parcel land", "Ha", "2026-01-01T00:00:02+00:00"),
        item("ig-0000000000000002", "second inbound", "ok", "2026-01-01T00:00:03+00:00"),
        item("ig-0000000000000003", "third inbound", "yo", "2026-01-01T00:00:04+00:00"),
    ]
    return {
        "schema_version": 1,
        "cases": [row[1] for row in rows],
        "references_by_case_id": {row[0]: row[2] for row in rows},
        "counts": {"cases": 3},
    }


class TallyTests(TestCase):
    def test_incomplete_runs_stay_out_of_the_ranking(self):
        candidate = {"slot": 0, "sample": 0, "status": "pending", "messages": [], "attempts": 0}
        report = tally([{
            "id": "abcd", "title": "Open", "complete": False, "vote": None,
            "lineup": [{"provider": "fake", "model": "lab-alpha"}] * 4,
            "candidates": [{**candidate, "slot": slot, "sample": sample}
                           for slot in range(4) for sample in range(4)],
        }])
        self.assertEqual(report["ranked_turns"], 0)
        self.assertEqual(report["models"], [])
        self.assertEqual(report["incomplete"][0]["pending"], 16)

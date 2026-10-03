"""Focused checks for the untrusted model-output boundary."""

import json
import asyncio
import tempfile
import unittest

from shadow.contracts import ContextEvent, DecisionInput
from shadow.decisions import ModelResponse, StructuredDecisions, parse_role
from shadow.model_bedrock import BedrockTransport
from shadow.settings import Settings
from shadow.storage import open_storage


class DecisionBoundaryTests(unittest.TestCase):
    def test_reply_accepts_exact_schema(self):
        raw = json.dumps(dict(schema_version=1, action="reply", reason_code="ordinary",
                              messages=["kal?"], evidence_event_ids=[4], used_memory_ids=[]))
        self.assertEqual(parse_role("reply", raw, {4}).value.messages, ("kal?",))

    def test_rejects_prose_duplicates_partial_and_unknown_ids(self):
        valid = '{"schema_version":1,"action":"skip","reason_code":"done","messages":[],"evidence_event_ids":[],"used_memory_ids":[]}'
        for raw in (valid + " extra", valid[:-1], valid.replace('"action":"skip"', '"action":"skip","action":"reply"'),
                    valid.replace('"evidence_event_ids":[]', '"evidence_event_ids":[9]'),
                    valid.replace('"messages":[]', '"messages":["no"]')):
            with self.subTest(raw=raw):
                self.assertEqual(parse_role("reply", raw, {4}).failure, "invalid_output")

    def test_rejects_overlong_and_wrong_types(self):
        raw = dict(schema_version=1, action="reply", reason_code="ordinary",
                   messages=["x" * 501], evidence_event_ids=[4], used_memory_ids=[])
        self.assertEqual(parse_role("reply", json.dumps(raw), {4}).failure, "invalid_output")
        raw["messages"] = [123]
        self.assertEqual(parse_role("reply", json.dumps(raw), {4}).failure, "invalid_output")

    def test_risk_requires_exact_fields_and_enums(self):
        raw = dict(schema_version=1, risk="distress", reason_code="urgent_distress",
                   action_hint="urgent", signals=[], evidence_event_ids=[4])
        self.assertEqual(parse_role("risk", json.dumps(raw), {4}).value.action_hint, "urgent")
        raw["risk"] = "safe enough"
        self.assertEqual(parse_role("risk", json.dumps(raw), {4}).failure, "invalid_output")
        raw["risk"] = "none"
        raw["signals"] = ["preference"]
        self.assertEqual(parse_role("risk", json.dumps(raw), {4}).failure, "invalid_output")

    def test_roles_budget_and_invalid_transport(self):
        class Fake:
            calls = []

            async def generate(self, role, system, input_text):
                self.calls.append(role)
                if role == "risk":
                    return ModelResponse(json.dumps(dict(schema_version=1, risk="none",
                         reason_code="ordinary", action_hint="proceed", signals=[],
                         evidence_event_ids=[4])), {}, "fake", "end_turn")
                return ModelResponse("{", {}, "fake", "end_turn")

        with tempfile.TemporaryDirectory() as directory, open_storage(directory) as storage:
            fake = Fake()
            decisions = StructuredDecisions(fake, storage, Settings(risk_call_budget=((1, 3600),)))
            data = DecisionInput(1, "known", {}, {}, (ContextEvent(4, "contact", "text", "hello"),), ())
            assessment = asyncio.run(decisions.assess(data))
            self.assertEqual(assessment.value.action_hint, "proceed")
            self.assertEqual(asyncio.run(decisions.draft(data, assessment.value)).failure, "invalid_output")
            self.assertEqual(asyncio.run(decisions.assess(data)).failure, "budget_exhausted")
            self.assertEqual(fake.calls, ["risk", "reply"])
            records = storage.conn.execute("SELECT role, failure, usage_json, versions_json FROM role_calls ORDER BY id").fetchall()
            self.assertEqual([row[0] for row in records], ["risk", "reply", "risk"])
            self.assertEqual(records[-1][1], "budget_exhausted")
            self.assertIn("prompt_digest", json.loads(records[0][3]))
            self.assertEqual(json.loads(records[0][2]), {})

    def test_bedrock_requires_models_and_passes_only_supported_arguments(self):
        class Client:
            def converse(self, **kw):
                self.kw = kw
                return {"stopReason": "end_turn", "output": {"message": {
                    "content": [{"text": "{}"}]}}, "usage": {"inputTokens": 3}}

        with self.assertRaises(ValueError):
            BedrockTransport(region="us-east-1", models={"reply": "x"})
        client = Client()
        transport = BedrockTransport(region="us-east-1", models=dict.fromkeys(
            ("risk", "reply", "extraction"), "profile-id"), client=client)
        result = asyncio.run(transport.generate("risk", "system", "data"))
        self.assertEqual(result.model, "profile-id")
        self.assertEqual(set(client.kw), {"modelId", "system", "messages", "inferenceConfig"})
        self.assertEqual(transport.configuration("risk")["transport_attempts"], 1)
        with self.assertRaises(ValueError):
            BedrockTransport(region="us-east-1", models=transport.models, client=client,
                             inference={"risk": {"temperature": 4}})
        with self.assertRaises(ValueError):
            asyncio.run(transport.generate("risk", "system", "x" * 32000))

    def test_strong_cues_can_raise_but_never_lower_risk(self):
        class UnsafeFake:
            async def generate(self, role, system, input_text):
                return ModelResponse(json.dumps(dict(schema_version=1, risk="none",
                     reason_code="ordinary", action_hint="proceed", signals=[],
                     evidence_event_ids=[4])), {}, "fake", "end_turn")

        with tempfile.TemporaryDirectory() as directory, open_storage(directory) as storage:
            decisions = StructuredDecisions(UnsafeFake(), storage, Settings())
            for text, expected in (("I want to kill myself lol", "urgent"),
                                   ("send me ₹500", "hold"),
                                   ("are you a bot?", "hold"),
                                   ("hi", "proceed")):
                with self.subTest(text=text):
                    data = DecisionInput(1, "known", {}, {},
                                         (ContextEvent(4, "contact", "text", text),), ())
                    outcome = asyncio.run(decisions.assess(data))
                    self.assertEqual(outcome.value.action_hint, expected)

    def test_extraction_receives_only_current_batch(self):
        class Capture:
            async def generate(self, role, system, input_text):
                self.input = json.loads(input_text)
                return ModelResponse('{"schema_version":1,"facts":[]}', {}, "fake", "end_turn")
        with tempfile.TemporaryDirectory() as directory, open_storage(directory) as storage:
            transport = Capture()
            data = DecisionInput(1, "known", {"secret": "relationship"},
                                 {"secret": "owner"},
                                 (ContextEvent(4, "contact", "text", "new"),),
                                 (ContextEvent(3, "contact", "text", "old"),))
            asyncio.run(StructuredDecisions(transport, storage, Settings()).extract(data))
            self.assertEqual(set(transport.input), {"current_batch", "subject_id"})

    def test_distress_beats_money_in_an_earlier_bubble_and_needs_no_provider(self):
        class Unavailable:
            async def generate(self, *args):
                raise AssertionError("explicit danger must not call a provider")
        with tempfile.TemporaryDirectory() as directory, open_storage(directory) as storage:
            data = DecisionInput(1, "known", {}, {}, (
                ContextEvent(4, "contact", "text", "send money"),
                ContextEvent(5, "contact", "text", "I want to kill myself")), ())
            result = asyncio.run(StructuredDecisions(Unavailable(), storage,
                Settings(risk_call_budget=((0, 3600),))).assess(data))
            self.assertEqual(result.value.action_hint, "urgent")
            self.assertEqual(storage.usage_counters(), [])


if __name__ == "__main__":
    unittest.main()

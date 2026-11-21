import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from shadow import storage
from shadow.context_memory import ContextMemory
from shadow.contracts import ContextEvent, DecisionInput, RiskAssessment
from shadow.decisions import StructuredDecisions, ModelResponse
from shadow.settings import Settings


class RecordingTransport:
    context_chars = 32000

    async def generate(self, role, system, input_text):
        self.prompt_chars = len(system) + len(input_text)
        self.payload = json.loads(input_text)
        evidence = self.payload.get("historical_evidence", [])
        return ModelResponse(json.dumps({
            "schema_version": 1, "action": "reply", "reason_code": "synthetic_reply",
            "messages": ["Did you end up going?"],
            "evidence_event_ids": [evidence[0]["event_id"]] if evidence else [],
            "used_memory_ids": [],
        }), {}, "offline", "end_turn")


class HistoricalRecallTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        # Seed the history before upgrading to the recall schema.
        with patch.object(storage, "MIGRATIONS", storage.MIGRATIONS[:11]):
            with storage.open_storage(directory) as db:
                self.cid = db.get_or_create_conversation("111", "222")
                db.set_contact_status(self.cid, "known")
                self.other = db.get_or_create_conversation("111", "333")
                self.hit = self.add(db, self.cid, "My pottery workshop is on Friday")
                self.update = self.add(db, self.cid, "It was canceled yesterday")
                self.foreign = self.add(db, self.other, "Pottery pottery pottery at a different venue")
                for _ in range(30):
                    self.add(db, self.cid, "Regular recent chat")
                self.inbound = self.add(db, self.cid, 'What happened with "pottery"?')
        self.db = self.enterContext(storage.open_storage(directory))
        self.transport = RecordingTransport()
        self.service = StructuredDecisions(self.transport, self.db, Settings())

    @staticmethod
    def add(db, cid, text):
        return db.add_event(conversation_id=cid, sender="contact", kind="text", text=text,
                            source="dom", origin="unknown")

    def draft_input(self):
        *earlier, current = self.db.get_events(self.cid)
        recent = tuple(ContextEvent(row["id"], row["sender"], row["kind"], row["text"])
                       for row in earlier)
        return DecisionInput(self.cid, "known", {}, {}, (
            ContextEvent(current["id"], current["sender"], current["kind"], current["text"]),
        ), recent)

    async def test_draft_can_cite_older_same_thread_evidence_and_its_update(self):
        result = await self.service.draft(self.draft_input(), RiskAssessment("none", "ok", "proceed"))
        self.assertIsNone(result.failure)
        payload = self.transport.payload
        self.assertEqual(len(payload["recent"]), 30)
        self.assertEqual([item["event_id"] for item in payload["historical_evidence"]],
                         [self.hit, self.update])
        self.assertEqual(result.value.evidence_event_ids, (self.hit,))

    async def test_forgotten_source_is_excluded_even_as_nearby_context(self):
        memory = ContextMemory(self.db)
        self.assertEqual(memory.save_candidates(self.cid, [{
            "subject_id": f"contact:{self.cid}", "kind": "preference",
            "value_text": "pottery workshop", "source_event_ids": [self.hit],
        }], {self.hit}), 1)
        self.assertTrue(memory.forget("fact", memory.facts(
            self.cid, storage.parse_iso(storage.utcnow()), candidates=True)[0]["id"]))
        result = await self.service.draft(self.draft_input(), RiskAssessment("none", "ok", "proceed"))
        self.assertIsNone(result.failure)
        self.assertNotIn("historical_evidence", self.transport.payload)
        self.assertEqual([row["id"] for row in self.db.get_events(self.cid)][:2],
                         [self.hit, self.update])
        hits = self.db.recall_events(self.cid, self.draft_input().recent[2].event_id, "canceled")
        self.assertEqual([item["event_id"] for item in hits], [self.update])

    async def test_historical_context_yields_to_base_prompt_budget(self):
        data = self.draft_input()
        assessment = RiskAssessment("none", "ok", "proceed")
        await self.service.draft(replace(data, recent=data.recent[-30:]), assessment)
        self.transport.context_chars = self.transport.prompt_chars
        result = await self.service.draft(data, assessment)
        self.assertIsNone(result.failure)
        self.assertNotIn("historical_evidence", self.transport.payload)
        self.assertEqual(len(self.transport.payload["recent"]), 30)

    def test_recall_uses_current_event_text_and_rejects_foreign_boundary(self):
        self.db.revise_event(self.cid, self.hit, metadata={"edited": True},
                             text="My ceramics workshop is on Friday")
        before = self.draft_input().recent[2].event_id
        self.assertEqual(self.db.recall_events(self.cid, before, "pottery"), [])
        hits = self.db.recall_events(self.cid, before, "ceramics")
        self.assertEqual([item["event_id"] for item in hits], [self.hit, self.update])
        self.assertEqual(self.db.recall_events(self.cid, self.foreign, "ceramics"), [])

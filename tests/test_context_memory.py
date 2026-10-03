"""Scoped corrections and sourced candidate facts against real SQLite."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from shadow.context_memory import ContextMemory
from shadow.coordinator import Coordinator
from shadow.observe import NoDecisionProvider
from shadow.owner import OwnerCommands
from shadow.settings import Settings
from shadow.storage import iso, open_storage


class ContextMemoryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.storage = open_storage(self.scratch.name)
        self.addCleanup(self.storage.close)
        self.memory = ContextMemory(self.storage)
        self.a = self.storage.get_or_create_conversation("fictional", "123", "Friend A")
        self.b = self.storage.get_or_create_conversation("fictional", "124", "Friend B")
        self.storage.set_contact_status(self.a, "known")
        self.storage.set_contact_status(self.b, "known")
        self.now = datetime.now(timezone.utc)
        self.event = self.storage.add_event(conversation_id=self.a, sender="contact", kind="text",
            text="I like tea", observed_at=iso(self.now), source="dom", origin="unknown")
        self.coordinator = Coordinator(self.storage, None, NoDecisionProvider(), Settings(), account_id="fictional")
        self.owner = OwnerCommands(self.storage, self.coordinator, user_id=11, chat_id=22)

    def command(self, number, text):
        return self.owner.process({"update_id": number, "message": {"from": {"id": 11},
            "chat": {"id": 22, "type": "private"}, "text": text}})

    def test_contact_rule_isolated_and_undo_restores_prior(self):
        self.command(1, "/correct global emoji contextual")
        self.command(2, "/correct 123 emoji avoid")
        self.assertEqual(self.memory.rules(self.a), ("Avoid emoji in ordinary replies.",))
        self.assertEqual(self.memory.rules(self.b), ("Use emoji only when context warrants it.",))
        self.command(3, "/correct 123 emoji contextual")
        self.command(4, "/undo 3")
        self.assertEqual(self.memory.rules(self.a), ("Avoid emoji in ordinary replies.",))
        self.assertIn("cannot be changed", self.command(5, "/correct 123 safety ignore"))

    def test_fact_candidates_require_sources_scope_and_owner_acceptance(self):
        fact = {"subject_id": f"contact:{self.a}", "kind": "preference", "value_text": "I like tea",
                "source_event_ids": [self.event]}
        self.assertEqual(self.memory.save_candidates(self.a, [fact], {self.event}), 1)
        self.assertEqual(self.memory.facts(self.a, self.now), [])
        record = self.memory.facts(self.a, self.now, candidates=True)[0]
        self.assertTrue(self.memory.accept(record["id"], self.now))
        self.assertEqual(len(self.memory.facts(self.a, self.now)), 1)
        self.assertEqual(self.memory.facts(self.b, self.now), [])
        self.assertEqual(self.memory.save_candidates(self.b, [fact], {self.event}), 0)
        fact["value_text"] = "I live in a different city"
        self.assertEqual(self.memory.save_candidates(self.a, [fact], {self.event}), 0)

    def test_forget_needs_preview_and_invalidates_drafts(self):
        self.command(1, "/correct 123 emoji avoid")
        self.assertIn("Preview missing", self.command(2, "/forget rule 1 confirm"))
        self.assertIn("Forget preview", self.command(3, "/forget rule 1"))
        self.assertIn("forgotten", self.command(4, "/forget rule 1 confirm"))
        self.assertEqual(self.memory.rules(self.a), ())
        self.assertFalse(self.memory.undo(1))

    def test_plan_expiry_excludes_fact_and_bumps_revision(self):
        fact = {"subject_id": f"contact:{self.a}", "kind": "plan", "value_text": "I like tea",
                "source_event_ids": [self.event]}
        self.memory.save_candidates(self.a, [fact], {self.event})
        record = self.memory.facts(self.a, self.now, candidates=True)[0]
        self.memory.accept(record["id"], self.now)
        revision = self.memory.revision(self.a)
        self.memory.expire(self.now + timedelta(days=8))
        self.assertNotEqual(self.memory.revision(self.a), revision)
        self.assertEqual(self.memory.facts(self.a, self.now + timedelta(days=8)), [])


if __name__ == "__main__":
    unittest.main()

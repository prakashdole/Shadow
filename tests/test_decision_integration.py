"""Decision persistence and stale-response checks through the real coordinator."""

import asyncio
import json
import tempfile
import unittest

from fake_ports import FakeBrowser, FakeDecisions, event, read
from shadow.contracts import RoleOutcome
from shadow.coordinator import Coordinator, TickReport
from shadow.settings import Settings
from shadow.storage import open_storage


class DecisionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.storage = open_storage(self.scratch.name)
        self.addCleanup(self.storage.close)
        self.cid = self.storage.get_or_create_conversation("fictional", "123", "Fictional Friend")
        self.storage.set_contact_status(self.cid, "known")
        self.storage.mark_baseline_done(self.cid)
        self.storage.add_event(conversation_id=self.cid, sender="contact", kind="text",
                               text="hello", source="dom", origin="unknown")
        self.storage.set_work(self.cid, inbound_revision=1, state="pending")

    async def test_draft_and_versions_survive_restart_without_outbox(self):
        class Versioned(FakeDecisions):
            async def draft(self, data, assessment):
                result = await super().draft(data, assessment)
                return RoleOutcome("reply", value=result.value, usage={"inputTokens": 8},
                                   versions={"model": "fictional-model"})
        coordinator = Coordinator(self.storage, FakeBrowser(), Versioned(), Settings(),
                                  account_id="fictional")
        await coordinator._assess(self.cid, TickReport())
        records = self.storage.decisions_for(self.cid)
        drafts = [record for record in records if "reply" in json.loads(record["record_json"])]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(json.loads(drafts[0]["record_json"])["reply"]["messages"], ["haan, 7 pm?"])
        self.assertEqual(json.loads(drafts[0]["versions_json"])["model"], "fictional-model")
        self.assertEqual(self.storage.outbox_items(), [])

    async def test_response_after_owner_pause_cannot_be_queued(self):
        class Pausing(FakeDecisions):
            async def draft(inner, data, assessment):
                coordinator.pause("owner")
                return await super().draft(data, assessment)
        coordinator = Coordinator(self.storage, FakeBrowser(), Pausing(),
                                  Settings(mode="autonomous", autonomy_scope="known"),
                                  account_id="fictional")
        await coordinator._assess(self.cid, TickReport())
        self.assertEqual(self.storage.outbox_items(), [])
        self.assertEqual(self.storage.get_work(self.cid)["state"], "held")

    async def test_response_for_changed_revision_is_discarded(self):
        class Changing(FakeDecisions):
            async def draft(inner, data, assessment):
                self.storage.update_work(self.cid, inbound_revision=2, state="pending")
                return await super().draft(data, assessment)
        coordinator = Coordinator(self.storage, FakeBrowser(), Changing(), Settings(),
                                  account_id="fictional")
        await coordinator._assess(self.cid, TickReport())
        self.assertEqual(self.storage.get_work(self.cid)["state"], "pending")
        self.assertEqual(self.storage.outbox_items(), [])

    async def test_current_batch_keeps_early_danger_beyond_outgoing_burst_cap(self):
        for index in range(7):
            self.storage.add_event(conversation_id=self.cid, sender="contact", kind="text",
                                   text=f"follow-up {index}", source="dom", origin="unknown")
        self.storage.update_work(self.cid, first_pending_seq=0)
        class Capturing(FakeDecisions):
            async def assess(inner, data):
                self.assertEqual(len(data.current_batch), 8)
                self.assertEqual(data.current_batch[0].text, "hello")
                return await super().assess(data)
        coordinator = Coordinator(self.storage, FakeBrowser(), Capturing(), Settings(), account_id="fictional")
        try:
            await coordinator._assess(self.cid, TickReport())
        finally:
            await coordinator.close()


if __name__ == "__main__":
    unittest.main()

"""Assisted request expiry, exact callbacks and once-only outbox enqueue."""

import json
import tempfile
import unittest
from datetime import datetime, timezone

from shadow.approvals import Approvals
from shadow.coordinator import Coordinator
from shadow.observe import NoDecisionProvider
from shadow.pacing import ManualClock
from shadow.settings import Settings
from shadow.storage import iso, open_storage


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.storage = open_storage(self.scratch.name)
        self.addCleanup(self.storage.close)
        self.clock = ManualClock(datetime(2026, 10, 3, 12, tzinfo=timezone.utc))
        self.coordinator = Coordinator(self.storage, None, NoDecisionProvider(),
            Settings(mode="assisted", timezone="UTC", active_hours={"daily": ("09:00", "23:00")}),
            clock=self.clock, account_id="fictional")
        self.cid = self.storage.get_or_create_conversation("fictional", "123", "Friend")
        self.storage.set_contact_status(self.cid, "known")
        self.storage.set_work(self.cid, inbound_revision=1, state="awaiting_approval", due_at=iso(self.clock.now()))
        self.decision = self.storage.add_decision(conversation_id=self.cid, source_revision=1,
            record_json=json.dumps({"reply": {"messages": ["Hello", "How are you?"]}}),
            versions_json="{}", status="awaiting_approval")
        self.approvals = Approvals(self.coordinator, allow_submission=True)
        self.request = self.approvals.create(self.decision)
        self.storage.conn.execute("UPDATE approval_requests SET message_id = 42 WHERE id = ?", (self.request,))

    def test_exact_message_and_default_lock(self):
        self.assertIn("out of date", self.approvals.act(self.request, "send", message_id=43))
        self.assertIn("locked", Approvals(self.coordinator).act(self.request, "send", message_id=42))
        self.assertEqual(self.storage.outbox_items(), [])

    def test_approval_enqueues_once_and_preserves_owner_approved_origin(self):
        self.assertIn("queued", self.approvals.act(self.request, "send", message_id=42))
        self.assertEqual(len(self.storage.outbox_items()), 2)
        self.assertTrue(all(item["authority"] == "owner_approved" for item in self.storage.outbox_items()))
        self.assertIn("out of date", self.approvals.act(self.request, "send", message_id=42))
        self.assertEqual(len(self.storage.outbox_items()), 2)

    def test_changed_revision_and_expiry_cannot_enqueue(self):
        self.storage.update_work(self.cid, inbound_revision=2)
        self.assertIn("out of date", self.approvals.act(self.request, "send"))
        self.assertEqual(self.storage.outbox_items(), [])

    def test_expiry_holds_and_edit_does_not_learn_automatically(self):
        self.assertIn("/edit-draft", self.approvals.act(self.request, "edit"))
        self.clock.advance(self.coordinator.settings.draft_ttl + 1)
        self.approvals.expire()
        self.assertEqual(self.storage.get_work(self.cid)["held_reason"], "approval_expired")
        self.assertEqual(self.storage.outbox_items(), [])
        self.assertEqual(self.storage.conn.execute("SELECT count(*) FROM correction_rules").fetchone()[0], 0)

    def test_recreating_same_request_does_not_supersede_or_extend_it(self):
        before = dict(self.storage.conn.execute("SELECT * FROM approval_requests WHERE id = ?", (self.request,)).fetchone())
        self.clock.advance(10)
        self.assertEqual(self.approvals.create(self.decision), self.request)
        after = dict(self.storage.conn.execute("SELECT * FROM approval_requests WHERE id = ?", (self.request,)).fetchone())
        self.assertEqual(before, after)

    def test_takeover_and_transport_total_hold_without_partial_enqueue(self):
        self.storage.set_owner_active_until(self.cid, "2099-01-01T00:00:00+00:00")
        self.assertIn("Held", self.approvals.act(self.request, "send"))
        self.storage.set_owner_active_until(self.cid, None)
        self.storage.conn.execute("UPDATE decisions SET record_json = ? WHERE id = ?",
            (json.dumps({"reply": {"messages": ["x" * 450] * 3}}), self.decision))
        self.assertIn("limits", self.approvals.act(self.request, "send"))
        self.assertEqual(self.storage.outbox_items(), [])


if __name__ == "__main__":
    unittest.main()

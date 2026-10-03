"""Owner identity and durable command outcomes, without Telegram network calls."""

import tempfile
import unittest

from shadow.coordinator import Coordinator
from shadow.observe import NoDecisionProvider
from shadow.owner import OwnerCommands, TelegramTransport
from shadow.settings import Settings
from shadow.storage import open_storage


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = open_storage(self.tmp.name)
        self.addCleanup(self.storage.close)
        self.coordinator = Coordinator(self.storage, None, NoDecisionProvider(),
                                       Settings(), account_id="acct-test")
        self.owner = OwnerCommands(self.storage, self.coordinator, user_id=101, chat_id=202)

    def update(self, number, text, *, user=101, chat=202, kind="private", **extra):
        return {"update_id": number, "message": {"from": {"id": user},
                "chat": {"id": chat, "type": kind}, "text": text} | extra}

    def test_authorization_dedup_and_persisted_pause(self):
        for number, kw in enumerate(({"user": 102}, {"chat": 203}, {"kind": "group"},
                                     {"forward_origin": {"type": "user"}}), 1):
            self.assertIsNone(self.owner.process(self.update(number, "/pause", **kw)))
        self.assertFalse(self.coordinator.paused)
        self.assertIn("Paused", self.owner.process(self.update(5, "/pause")))
        self.assertIsNone(self.owner.process(self.update(5, "/resume")))
        self.assertEqual(self.storage.last_owner_update_id(), 5)
        self.assertEqual(self.storage.pending_owner_responses()[0]["update_id"], 5)
        self.assertIn("cleared", self.owner.process(self.update(6, "/resume")))
        self.assertFalse(self.coordinator.paused)
        self.coordinator.pause("challenge")
        self.assertIn("Refused", self.owner.process(self.update(7, "/resume")))
        self.assertTrue(self.coordinator.paused)

    def test_exact_contact_and_no_send_or_autonomy(self):
        cid = self.storage.get_or_create_conversation("acct-test", "123", "Friend")
        self.storage.set_work(cid, inbound_revision=1, state="pending")
        self.assertIn("no change", self.owner.process(self.update(1, "/approve 12")))
        self.assertEqual(self.storage.get_conversation(cid)["contact_status"], "unknown_pending")
        self.assertIn("approved", self.owner.process(self.update(2, "/approve 123")))
        self.assertIn("Refused", self.owner.process(self.update(3, "/mode autonomous")))
        self.assertIn("no Instagram send", self.owner.process(self.update(4, "/send 123 hi")))
        self.assertIn("excluded", self.owner.process(self.update(5, "/ignore 123")))
        self.assertEqual(self.storage.get_work(cid)["held_reason"], "contact_excluded")

    def test_exact_thread_pause_scope_and_safe_resume(self):
        cid = self.storage.get_or_create_conversation("acct-test", "123", "Friend")
        other = self.storage.get_or_create_conversation("acct-test", "456", "Friend")
        self.storage.set_contact_status(cid, "known")
        self.storage.set_contact_status(other, "known")
        self.storage.set_work(cid, inbound_revision=1, state="pending")
        self.assertIn("Scope updated", self.owner.process(self.update(1, "/scope add 123")))
        self.assertTrue(self.coordinator.in_autonomy_scope(self.storage.get_conversation(cid)))
        self.assertFalse(self.coordinator.in_autonomy_scope(self.storage.get_conversation(other)))
        self.assertIn("paused", self.owner.process(self.update(2, "/pause 123")))
        self.assertEqual(self.storage.get_conversation(cid)["paused_reason"], "owner_pause")
        self.assertIn("cleared", self.owner.process(self.update(3, "/resume 123")))
        self.assertEqual(self.storage.get_work(cid)["state"], "pending")
        self.storage.update_work(cid, state="held", held_reason="uncertain_send")
        self.assertIn("Refused", self.owner.process(self.update(4, "/resume 123")))
        self.assertEqual(self.storage.get_work(cid)["state"], "held")

    def test_failed_ack_is_retried_without_reexecuting_command(self):
        self.owner.process(self.update(1, "/pause"))
        class FakeTransport(TelegramTransport):
            def __init__(self):
                self.calls = []
            def _post(self, method, **fields):
                self.calls.append(method)
                if method == "getUpdates":
                    return []
                return {}
        fake = FakeTransport()
        fake.poll_once(self.owner)
        self.assertEqual(fake.calls, ["getUpdates", "sendMessage"])
        self.assertEqual(self.storage.pending_owner_responses(), [])
        self.assertIsNone(self.owner.process(self.update(1, "/resume")))
        self.assertTrue(self.coordinator.paused)


if __name__ == "__main__":
    unittest.main()

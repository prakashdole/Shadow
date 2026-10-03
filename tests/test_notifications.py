"""Durable delivery/repeat/ack behaviour without external messaging."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from shadow.notifications import Notifications, digest_text
from shadow.owner import OwnerCommands, message_pages
from shadow.coordinator import Coordinator
from shadow.observe import NoDecisionProvider
from shadow.pacing import ManualClock
from shadow.settings import Settings
from shadow.storage import open_storage


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.storage = open_storage(self.scratch.name)
        self.addCleanup(self.storage.close)
        self.clock = ManualClock(datetime(2026, 10, 3, 2, tzinfo=timezone.utc))
        self.settings = Settings(timezone="UTC", active_hours={"daily": ("09:00", "23:00")})
        class Transport:
            fail = False
            calls = []
            def _post(inner, method, **fields):
                if inner.fail:
                    raise RuntimeError("offline")
                inner.calls.append(fields)
                return {}
        self.transport = Transport()
        self.transport.calls = []
        self.notifications = Notifications(self.storage, self.transport, self.settings,
                                           chat_id=22, secondary_chat_id=33, clock=self.clock)

    async def test_tiers_repeat_escalation_and_ack_does_not_resume(self):
        urgent = self.storage.raise_alert(dedupe_key="urgent", tier="urgent", reason="challenge")
        self.storage.raise_alert(dedupe_key="action", tier="action", reason="unknown_contact")
        self.storage.raise_alert(dedupe_key="info", tier="info", reason="group")
        await self.notifications.deliver_due()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.storage.alerts()[0]["state"], "sent")
        await self.notifications.deliver_due()
        self.assertEqual(len(self.transport.calls), 1)
        self.clock.advance(self.settings.secondary_alert_after)
        await self.notifications.deliver_due()
        self.assertTrue(any(call["chat_id"] == 33 for call in self.transport.calls))
        coordinator = Coordinator(self.storage, None, NoDecisionProvider(), self.settings,
                                  clock=self.clock, account_id="fictional")
        coordinator.pause("challenge")
        owner = OwnerCommands(self.storage, coordinator, user_id=11, chat_id=22)
        owner.process({"update_id": 1, "message": {"from": {"id": 11},
            "chat": {"id": 22, "type": "private"}, "text": f"/ack {urgent}"}})
        before = len(self.transport.calls)
        self.clock.advance(self.settings.urgent_repeat_schedule[-1])
        await self.notifications.deliver_due()
        self.assertEqual(len(self.transport.calls), before)
        self.assertTrue(coordinator.paused)

    async def test_outage_preserves_pending_and_retries_at_due_time(self):
        self.storage.raise_alert(dedupe_key="u", tier="urgent", reason="challenge")
        self.transport.fail = True
        await self.notifications.deliver_due()
        self.assertEqual(self.storage.alerts()[0]["state"], "pending")
        self.assertEqual(self.storage.alerts()[0]["attempts"], 1)
        self.transport.fail = False
        await self.notifications.deliver_due()
        self.assertEqual(self.transport.calls, [])
        self.clock.advance(self.settings.idle_backoff_max)
        await self.notifications.deliver_due()
        self.assertEqual(len(self.transport.calls), 1)

    def test_pages_preserve_unicode_and_digest_includes_info(self):
        text = "😀" * 6000
        pages = list(message_pages(text))
        self.assertEqual("".join(pages), text)
        self.assertTrue(all(len(page.encode("utf-16-le")) <= 8000 for page in pages))
        self.storage.raise_alert(dedupe_key="i", tier="info", reason="baseline_unanswered")
        self.assertIn("baseline_unanswered", digest_text(self.storage, now=self.clock.now(), settings=self.settings))


if __name__ == "__main__":
    unittest.main()

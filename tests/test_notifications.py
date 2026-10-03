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

    async def test_digest_delivery_once_and_failed_delivery_keeps_info(self):
        info = self.storage.raise_alert(dedupe_key="info", tier="info", reason="baseline_unanswered")
        await self.notifications.deliver_digest()
        self.assertEqual(self.transport.calls, [])
        self.clock.current = self.clock.now().replace(hour=23)
        self.transport.fail = True
        await self.notifications.deliver_digest()
        self.assertEqual(next(a for a in self.storage.alerts() if a["id"] == info)["digest_included"], 0)
        self.transport.fail = False
        self.clock.advance(300)
        await self.notifications.deliver_digest()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertIn("7-day metrics", self.transport.calls[0]["text"])
        await self.notifications.deliver_digest()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(next(a for a in self.storage.alerts() if a["id"] == info)["digest_included"], 1)

    async def test_service_retry_hint_is_not_shortened_to_local_backoff(self):
        from shadow.owner import TelegramUnavailable

        self.storage.raise_alert(dedupe_key="retry", tier="urgent", reason="challenge")
        def fail(*args, **kwargs):
            raise TelegramUnavailable(600)
        self.transport._post = fail
        await self.notifications.deliver_due()
        from shadow.storage import parse_iso

        retry = parse_iso(self.storage.alerts()[0]["next_attempt_at"])
        self.assertEqual((retry - self.clock.now()).total_seconds(), 600)


if __name__ == "__main__":
    unittest.main()

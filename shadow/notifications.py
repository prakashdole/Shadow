"""Durable reason-only alerts. Delivery is not acknowledgement or resolution."""

import asyncio
from datetime import timedelta

from shadow.pacing import SystemClock, in_active_hours
from shadow.storage import iso, parse_iso


class Notifications:
    def __init__(self, storage, transport, settings, *, chat_id, secondary_chat_id=None, clock=None):
        if type(chat_id) is not int or chat_id <= 0:
            raise ValueError("private owner chat ID required")
        if secondary_chat_id is not None and (type(secondary_chat_id) is not int or secondary_chat_id <= 0):
            raise ValueError("private secondary chat ID required")
        self.storage, self.transport, self.settings = storage, transport, settings
        self.chat_id, self.secondary_chat_id = chat_id, secondary_chat_id
        self.clock = clock or SystemClock()

    async def _send(self, chat_id, text):
        return await asyncio.to_thread(self.transport._post, "sendMessage", chat_id=chat_id, text=text)

    async def deliver_due(self):
        now = self.clock.now()
        for alert in self.storage.alerts():
            if alert["state"] in ("acknowledged", "resolved") or alert["tier"] == "info":
                continue
            due = parse_iso(alert["next_attempt_at"])
            if due is not None and due > now:
                continue
            if alert["tier"] == "action" and not (
                self.settings.active_hours and self.settings.timezone
                and in_active_hours(now, self.settings)
            ):
                continue
            sent_at = parse_iso(alert["sent_at"])
            if sent_at is not None:
                if alert["tier"] != "urgent":
                    continue
                index = alert["repeat_index"]
                schedule = self.settings.urgent_repeat_schedule
                if index >= len(schedule) or now < sent_at + timedelta(seconds=schedule[index]):
                    await self._escalate(alert, now, sent_at)
                    continue
            text = (f"Shadow {alert['tier']} alert #{alert['id']}: {alert['reason']}. "
                    f"Thread ref={alert['conversation_id'] or 'account'}. "
                    f"/ack {alert['id']} acknowledges only; it does not clear the hold.")
            try:
                await self._send(self.chat_id, text)
            except RuntimeError:
                self._retry(alert, now)
                continue
            current = next(item for item in self.storage.alerts() if item["id"] == alert["id"])
            if current["state"] in ("acknowledged", "resolved"):
                continue
            fields = {"state": "sent", "attempts": alert["attempts"] + 1,
                      "next_attempt_at": None}
            if sent_at is None:
                fields["sent_at"] = iso(now)
            else:
                fields["repeat_index"] = alert["repeat_index"] + 1
            self.storage.update_alert_delivery(alert["id"], **fields)
            await self._escalate(alert, now, sent_at or now)

    def _retry(self, alert, now):
        # Bound retry delays by the existing idle backoff setting.
        delay = min(self.settings.idle_backoff_max,
                    max(1, self.settings.pane_check_interval) * 2 ** min(alert["attempts"], 8))
        self.storage.update_alert_delivery(alert["id"], attempts=alert["attempts"] + 1,
                                           next_attempt_at=iso(now + timedelta(seconds=delay)))

    async def _escalate(self, alert, now, sent_at):
        if (alert["tier"] != "urgent" or not self.secondary_chat_id or alert["escalated_at"]
                or now < sent_at + timedelta(seconds=self.settings.secondary_alert_after)):
            return
        try:
            await self._send(self.secondary_chat_id,
                             f"Shadow urgent alert #{alert['id']} remains unacknowledged; owner action required.")
        except RuntimeError:
            return
        self.storage.update_alert_delivery(alert["id"], escalated_at=iso(now))


def digest_text(storage, *, now, settings):
    """One day in the owner's timezone; confirmed items retain their exact text."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(settings.timezone) if settings.timezone else now.tzinfo
    today = now.astimezone(zone).date()
    lines = [f"Shadow digest {today.isoformat()}"]
    for item in storage.outbox_items():
        confirmed = parse_iso(item["confirmed_at"])
        if item["state"] == "confirmed" and confirmed and confirmed.astimezone(zone).date() == today:
            lines.append(f"Sent #{item['id']} thread ref={item['conversation_id']}: {item['text']}")
    for work in storage.work_in_states(("held", "owner_active", "awaiting_approval")):
        lines.append(f"Unanswered thread ref={work['conversation_id']}: {work['held_reason'] or work['state']}")
    for alert in storage.alerts():
        if alert["tier"] == "info" and not alert["digest_included"]:
            lines.append(f"Info #{alert['id']}: {alert['reason']}")
    return "\n".join(lines)

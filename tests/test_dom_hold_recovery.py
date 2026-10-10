"""Regression coverage for recovering a transient pre-send DOM hold.

These checks use the real coordinator and disposable SQLite state.  The browser
and decision boundary are fictional doubles, so no browser is ever driven.
"""

from __future__ import annotations

import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fake_ports import FakeBrowser, FakeDecisions, event, read, row
from shadow import storage as ST
from shadow.coordinator import Coordinator
from shadow.contracts import ObservedEvent
from shadow.pacing import ManualClock
from shadow.settings import Settings


UTC = timezone.utc
NOW = datetime(2026, 10, 10, 4, 30, tzinfo=UTC)
ACCOUNT = "acct-dom-hold"
THREAD = "100200300"


def settings() -> Settings:
    return Settings(
        mode="autonomous",
        timezone="Asia/Kolkata",
        active_hours={"daily": ("09:00", "23:00")},
        autonomy_scope="known",
    )


class DomHoldRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="shadow-dom-hold-"))
        self.addCleanup(self._cleanup)
        self.storage = ST.open_storage(self.directory)
        self.addCleanup(self.storage.close)
        self.browser = FakeBrowser()
        self.decisions = FakeDecisions()
        self.clock = ManualClock(NOW)
        self.coordinator = Coordinator(
            self.storage, self.browser, self.decisions, settings(),
            clock=self.clock, rng=random.Random(1), account_id=ACCOUNT,
        )

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)

    def conversation(self, *, status: str = "known") -> int:
        conversation_id = self.storage.get_or_create_conversation(ACCOUNT, THREAD, "Test Contact")
        self.storage.set_contact_status(conversation_id, status, evidence="test")
        self.storage.add_event(
            conversation_id=conversation_id, sender="contact", kind="text", text="hi",
            observed_at=ST.iso(self.clock.now()), source="dom", origin="unknown",
        )
        self.storage.mark_baseline_done(conversation_id)
        return conversation_id

    def unseen_conversation(self) -> int:
        conversation_id = self.storage.get_or_create_conversation(ACCOUNT, THREAD, "Test Contact")
        self.storage.set_contact_status(conversation_id, "known", evidence="test")
        return conversation_id

    def queue_due_work(self, conversation_id: int, *, items: int = 1) -> None:
        work = self.storage.get_work(conversation_id)
        if work is None:
            self.storage.set_work(
                conversation_id, inbound_revision=1, due_at=ST.iso(self.clock.now()),
                first_pending_at=ST.iso(self.clock.now()), state="due",
            )
            work = self.storage.get_work(conversation_id)
        self.storage.update_work(
            conversation_id, state="due", due_at=ST.iso(self.clock.now()),
            first_pending_seq=1,
        )
        decision_id = self.storage.add_decision(
            conversation_id=conversation_id, source_revision=1, record_json="{}",
            versions_json="{}", status="queued",
        )
        for index in range(items):
            self.storage.add_outbox_item(
                decision_id=decision_id, item_index=index, conversation_id=conversation_id,
                text=f"draft {index}", authority="autonomous", revision=1,
            )

    async def hold_on_dom_unknown(self, conversation_id: int) -> None:
        self.browser.reads[THREAD] = read(THREAD, status="dom_unknown")
        await self.coordinator.tick_due()
        work = self.storage.get_work(conversation_id)
        self.assertEqual((work["state"], work["held_reason"]), ("held", "reread_dom_unknown"))

    async def test_healthy_exact_window_reassesses_original_pending_inbound(self) -> None:
        conversation_id = self.conversation()
        self.queue_due_work(conversation_id)
        await self.hold_on_dom_unknown(conversation_id)

        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(THREAD, (event(0, "contact", "hi"),))
        report = await self.coordinator.tick_discover()

        old_item = self.storage.outbox_items(conversation_id)[0]
        work = self.storage.get_work(conversation_id)
        self.assertEqual(old_item["state"], "cancelled")
        self.assertEqual(work["inbound_revision"], 2)
        self.assertNotEqual(work["state"], "held")
        self.assertEqual(report.ingested, 0)
        self.assertEqual(self.decisions.calls[:2], [("assess", conversation_id), ("draft", conversation_id)])
        self.assertEqual([entry["text"] for entry in self.storage.get_events(conversation_id)], ["hi"])
        self.assertEqual(self.browser.sent, [])

    async def test_partial_burst_never_recovers_or_regenerates_confirmed_prefix(self) -> None:
        conversation_id = self.conversation()
        self.queue_due_work(conversation_id, items=2)
        items = self.storage.outbox_items(conversation_id)
        self.storage.transition(items[0]["id"], "attempting")
        self.storage.confirm_send(items[0]["id"], {
            "text": "draft 0", "observed_at": ST.iso(self.clock.now()),
        })
        await self.hold_on_dom_unknown(conversation_id)

        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(
            THREAD, (event(0, "contact", "hi"), event(1, "owner", "draft 0")),
        )
        await self.coordinator.tick_discover()

        self.assertEqual([item["state"] for item in self.storage.outbox_items(conversation_id)],
                         ["confirmed", "pending"])
        self.assertEqual(self.storage.get_work(conversation_id)["held_reason"], "reread_dom_unknown")
        self.assertEqual(self.decisions.calls, [])
        self.assertEqual(self.browser.sent, [])

    async def test_earlier_confirmed_reply_does_not_block_current_pending_recovery(self) -> None:
        conversation_id = self.conversation()
        old_decision = self.storage.add_decision(
            conversation_id=conversation_id, source_revision=1, record_json="{}",
            versions_json="{}", status="queued",
        )
        old_item = self.storage.add_outbox_item(
            decision_id=old_decision, item_index=0, conversation_id=conversation_id,
            text="earlier answer", authority="autonomous", revision=1,
        )
        self.storage.transition(old_item, "attempting")
        self.storage.confirm_send(old_item, {
            "text": "earlier answer", "observed_at": ST.iso(self.clock.now()),
        })
        self.storage.set_decision_status(old_decision, "done")
        self.storage.add_event(
            conversation_id=conversation_id, sender="contact", kind="text", text="new inbound",
            observed_at=ST.iso(self.clock.now()), source="dom", origin="unknown",
        )
        self.storage.set_work(
            conversation_id, inbound_revision=2, due_at=ST.iso(self.clock.now()),
            first_pending_at=ST.iso(self.clock.now()), state="due",
        )
        self.storage.update_work(conversation_id, first_pending_seq=3)
        current_decision = self.storage.add_decision(
            conversation_id=conversation_id, source_revision=2, record_json="{}",
            versions_json="{}", status="queued",
        )
        current_item = self.storage.add_outbox_item(
            decision_id=current_decision, item_index=0, conversation_id=conversation_id,
            text="stale current draft", authority="autonomous", revision=2,
        )
        await self.hold_on_dom_unknown(conversation_id)

        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(
            THREAD, (event(0, "contact", "hi"), event(1, "owner", "earlier answer"),
                     event(2, "contact", "new inbound")),
        )
        await self.coordinator.tick_discover()

        items = {item["id"]: item for item in self.storage.outbox_items(conversation_id)}
        self.assertEqual(items[old_item]["state"], "confirmed")
        self.assertEqual(items[current_item]["state"], "cancelled")
        self.assertNotEqual(self.storage.get_work(conversation_id)["state"], "held")
        self.assertEqual(self.decisions.calls[:2], [("assess", conversation_id), ("draft", conversation_id)])

    async def test_unknown_contact_does_not_recover_a_dom_hold(self) -> None:
        conversation_id = self.conversation(status="unknown_pending")
        self.queue_due_work(conversation_id)
        await self.hold_on_dom_unknown(conversation_id)

        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(THREAD, (event(0, "contact", "hi"),))
        await self.coordinator.tick_discover()

        self.assertEqual(self.storage.get_work(conversation_id)["held_reason"], "reread_dom_unknown")
        self.assertEqual(self.decisions.calls, [])

    async def test_baseline_or_owner_history_is_not_reassessed_as_pending_inbound(self) -> None:
        conversation_id = self.storage.get_or_create_conversation(ACCOUNT, THREAD, "Test Contact")
        self.storage.set_contact_status(conversation_id, "known", evidence="test")
        for sender, text in (("contact", "old inbound"), ("owner", "old answer")):
            self.storage.add_event(
                conversation_id=conversation_id, sender=sender, kind="text", text=text,
                observed_at=ST.iso(self.clock.now()), source="dom", origin="unknown",
            )
        self.storage.mark_baseline_done(conversation_id)
        self.storage.set_work(conversation_id, inbound_revision=1, state="held")
        self.storage.update_work(conversation_id, held_reason="reread_dom_unknown")
        decision_id = self.storage.add_decision(
            conversation_id=conversation_id, source_revision=1, record_json="{}",
            versions_json="{}", status="queued",
        )
        self.storage.add_outbox_item(
            decision_id=decision_id, item_index=0, conversation_id=conversation_id,
            text="old draft", authority="autonomous", revision=1,
        )

        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(
            THREAD, (event(0, "contact", "old inbound"), event(1, "owner", "old answer")),
        )
        await self.coordinator.tick_discover()

        self.assertEqual(self.storage.get_work(conversation_id)["held_reason"], "reread_dom_unknown")
        self.assertEqual(self.storage.outbox_items(conversation_id)[0]["state"], "pending")
        self.assertEqual(self.decisions.calls, [])

    async def test_observe_mode_recovers_without_sending(self) -> None:
        self.coordinator = Coordinator(
            self.storage, self.browser, self.decisions,
            Settings(mode="observe"), clock=self.clock, rng=random.Random(1), account_id=ACCOUNT,
        )
        conversation_id = self.conversation()
        self.queue_due_work(conversation_id)
        self.storage.update_work(conversation_id, state="held", held_reason="reread_dom_unknown")

        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(THREAD, (event(0, "contact", "hi"),))
        await self.coordinator.tick_discover()

        self.assertEqual(self.storage.outbox_items(conversation_id)[0]["state"], "cancelled")
        self.assertEqual(self.storage.get_work(conversation_id)["state"], "idle")
        self.assertEqual(self.browser.sent, [])

    async def test_unknown_sender_baseline_stays_idle_then_healthy_window_is_history(self) -> None:
        conversation_id = self.unseen_conversation()
        unknown = ObservedEvent(0, "unknown", "text", "same text")
        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(THREAD, (unknown,))
        await self.coordinator.tick_discover()

        self.assertFalse(self.storage.get_conversation(conversation_id)["first_seen_baseline_done"])
        self.assertEqual(self.storage.get_events(conversation_id), [])
        self.assertEqual(self.storage.get_work(conversation_id)["state"], "idle")
        self.assertIsNone(self.storage.get_conversation(conversation_id)["paused_reason"])
        self.assertEqual(len(self.storage.dom_observations(conversation_id)), 1)

        self.browser.reads[THREAD] = read(THREAD, (event(0, "contact", "same text"),))
        report = await self.coordinator.tick_discover()

        self.assertTrue(self.storage.get_conversation(conversation_id)["first_seen_baseline_done"])
        self.assertEqual([entry["sender"] for entry in self.storage.get_events(conversation_id)], ["contact"])
        self.assertEqual(report.ingested, 1)
        self.assertEqual(self.decisions.calls, [])

    async def test_unknown_kind_and_unlaid_baselines_are_not_canonicalized(self) -> None:
        for suspect in (
            ObservedEvent(0, "contact", "unknown", ""),
            ObservedEvent(0, "contact", "text", "unlaid", laid_out=False),
        ):
            with self.subTest(suspect=suspect):
                self.storage.close()
                self.storage = ST.open_storage(self.directory)
                self.coordinator = Coordinator(
                    self.storage, self.browser, self.decisions, settings(),
                    clock=self.clock, rng=random.Random(1), account_id=ACCOUNT,
                )
                conversation_id = self.unseen_conversation()
                self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
                self.browser.reads[THREAD] = read(THREAD, (suspect,))
                await self.coordinator.tick_discover()

                self.assertFalse(self.storage.get_conversation(conversation_id)["first_seen_baseline_done"])
                self.assertEqual(self.storage.get_events(conversation_id), [])

    async def test_unreadable_new_tail_stays_idle_until_one_healthy_ingestion(self) -> None:
        conversation_id = self.conversation()
        unreadable = ObservedEvent(1, "unknown", "text", "new inbound")
        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(THREAD, (event(0, "contact", "hi"), unreadable))
        await self.coordinator.tick_discover()

        self.assertEqual([entry["text"] for entry in self.storage.get_events(conversation_id)], ["hi"])
        self.assertEqual(self.storage.get_work(conversation_id)["state"], "idle")

        self.browser.reads[THREAD] = read(
            THREAD, (event(0, "contact", "hi"), event(1, "contact", "new inbound")),
        )
        report = await self.coordinator.tick_discover()

        self.assertEqual([entry["text"] for entry in self.storage.get_events(conversation_id)], ["hi", "new inbound"])
        self.assertEqual(report.ingested, 1)

    async def test_healthy_exact_window_recovers_a_transient_unreadable_pre_send_hold(self) -> None:
        conversation_id = self.conversation()
        self.queue_due_work(conversation_id)
        self.browser.reads[THREAD] = read(THREAD, (ObservedEvent(0, "unknown", "text", "hi"),))
        await self.coordinator.tick_due()
        self.assertEqual(self.storage.get_work(conversation_id)["held_reason"], "reread_unreadable_dom")

        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(THREAD, (event(0, "contact", "hi"),))
        await self.coordinator.tick_discover()

        self.assertEqual(self.storage.outbox_items(conversation_id)[0]["state"], "cancelled")
        self.assertNotEqual(self.storage.get_work(conversation_id)["state"], "held")
        self.assertEqual(self.browser.sent, [])

    async def test_healthy_exact_window_reassesses_a_pre_draft_readability_hold(self) -> None:
        conversation_id = self.conversation()
        self.storage.set_work(
            conversation_id, inbound_revision=1, due_at=ST.iso(self.clock.now()),
            first_pending_at=ST.iso(self.clock.now()), state="pending",
        )
        self.storage.update_work(conversation_id, first_pending_seq=1)
        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(THREAD, (ObservedEvent(0, "unknown", "text", "hi"),))
        await self.coordinator.tick_discover()
        self.assertEqual(self.storage.get_work(conversation_id)["held_reason"], "reread_unreadable_dom")
        self.assertEqual(self.decisions.calls, [])

        self.browser.reads[THREAD] = read(THREAD, (event(0, "contact", "hi"),))
        await self.coordinator.tick_discover()

        self.assertNotEqual(self.storage.get_work(conversation_id)["state"], "held")
        self.assertEqual(self.storage.get_work(conversation_id)["first_pending_seq"], 1)
        self.assertEqual(self.decisions.calls[:2], [("assess", conversation_id), ("draft", conversation_id)])

    async def test_unreadable_read_never_overwrites_a_stronger_hold(self) -> None:
        conversation_id = self.conversation()
        self.storage.set_work(conversation_id, inbound_revision=1, state="held")
        self.storage.update_work(conversation_id, held_reason="risk_money", first_pending_seq=1)
        self.storage.set_paused_reason(conversation_id, "risk_money")
        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(THREAD, (ObservedEvent(0, "unknown", "text", "hi"),))
        await self.coordinator.tick_discover()

        self.assertEqual(self.storage.get_work(conversation_id)["held_reason"], "risk_money")
        self.assertEqual(self.storage.get_conversation(conversation_id)["paused_reason"], "risk_money")

    async def test_unreadable_read_never_reopens_completed_work(self) -> None:
        conversation_id = self.conversation()
        self.storage.set_work(conversation_id, inbound_revision=1, state="idle")
        self.storage.update_work(
            conversation_id, first_pending_seq=1, first_pending_at=ST.iso(self.clock.now())
        )
        decision_id = self.storage.add_decision(
            conversation_id=conversation_id, source_revision=1, record_json="{}",
            versions_json="{}", status="queued",
        )
        item_id = self.storage.add_outbox_item(
            decision_id=decision_id, item_index=0, conversation_id=conversation_id,
            text="draft 0", authority="autonomous", revision=1,
        )
        self.storage.transition(item_id, "attempting")
        self.storage.confirm_send(item_id, {
            "text": "draft 0", "observed_at": ST.iso(self.clock.now()),
        })
        self.storage.set_decision_status(decision_id, "done")
        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(
            THREAD, (ObservedEvent(0, "unknown", "text", "hi"),),
        )
        await self.coordinator.tick_discover()

        self.assertEqual(self.storage.get_work(conversation_id)["state"], "idle")
        self.assertEqual(self.decisions.calls, [])

        self.browser.reads[THREAD] = read(
            THREAD, (event(0, "contact", "hi"), event(1, "owner", "draft 0")),
        )
        await self.coordinator.tick_discover()

        self.assertEqual(self.storage.get_work(conversation_id)["state"], "idle")
        self.assertEqual([item["state"] for item in self.storage.outbox_items(conversation_id)],
                         ["confirmed"])
        self.assertEqual(self.decisions.calls, [])

    async def test_unknown_sender_system_or_reaction_does_not_reject_readable_baseline(self) -> None:
        conversation_id = self.unseen_conversation()
        system = ObservedEvent(1, "unknown", "system", "messages are encrypted")
        reaction = ObservedEvent(2, "unknown", "reaction", "👍")
        self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
        self.browser.reads[THREAD] = read(
            THREAD, (event(0, "contact", "hi"), system, reaction),
        )
        await self.coordinator.tick_discover()

        self.assertTrue(self.storage.get_conversation(conversation_id)["first_seen_baseline_done"])
        self.assertEqual([entry["kind"] for entry in self.storage.get_events(conversation_id)],
                         ["text", "system"])

    async def test_group_uncertain_and_manual_composer_never_canonicalize(self) -> None:
        for result in (
            read(THREAD, (event(0, "contact", "hi"),), is_group=None),
            read(THREAD, (event(0, "contact", "hi"),), composer_text="manual"),
        ):
            with self.subTest(result=result):
                self.storage.close()
                self.storage = ST.open_storage(self.directory)
                self.coordinator = Coordinator(
                    self.storage, self.browser, self.decisions, settings(),
                    clock=self.clock, rng=random.Random(1), account_id=ACCOUNT,
                )
                conversation_id = self.unseen_conversation()
                self.browser.inbox_result = type(self.browser.inbox_result)(status="ok", rows=(row(0),))
                self.browser.reads[THREAD] = result
                await self.coordinator.tick_discover()

                self.assertFalse(self.storage.get_conversation(conversation_id)["first_seen_baseline_done"])
                self.assertEqual(self.storage.get_events(conversation_id), [])

    async def test_pre_send_unreadable_or_wrong_thread_never_stores_or_sends(self) -> None:
        for result in (
            read(THREAD, (ObservedEvent(0, "unknown", "text", "hi"),)),
            read("999999999", (event(0, "contact", "hi"),)),
            read(THREAD, (event(0, "contact", "hi"),), composer_text="manual"),
        ):
            with self.subTest(result=result):
                self.storage.close()
                self.storage = ST.open_storage(self.directory)
                self.coordinator = Coordinator(
                    self.storage, self.browser, self.decisions, settings(),
                    clock=self.clock, rng=random.Random(1), account_id=ACCOUNT,
                )
                conversation_id = self.conversation()
                self.queue_due_work(conversation_id)
                self.browser.reads[THREAD] = result
                before = self.storage.get_events(conversation_id)
                await self.coordinator.tick_due()

                self.assertEqual(self.storage.get_events(conversation_id), before)
                self.assertEqual(self.browser.sent, [])


if __name__ == "__main__":
    unittest.main()

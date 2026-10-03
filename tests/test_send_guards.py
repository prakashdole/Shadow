"""Focused offline regression checks for the last-moment send boundary."""

from dataclasses import replace

from fake_ports import event, read
from test_coordinator import CoordinatorCase, THREAD_A
from shadow.coordinator import TickReport


class SendGuards(CoordinatorCase):
    async def test_unknown_group_and_wrong_thread_never_send(self):
        cid = self.conversation()
        self.queue_due_send(cid, "hello")
        self.browser.reads[THREAD_A] = read(THREAD_A, (event(0, "contact", "hi"),), is_group=None)
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.work(cid)["held_reason"], "group_uncertain")
        self.storage.update_work(cid, state="due")
        self.browser.reads[THREAD_A] = read("999", (event(0, "contact", "hi"),))
        await self.coordinator.tick_due()
        self.assertEqual(self.work(cid)["held_reason"], "wrong_thread")

    async def test_fresh_inbound_between_bubbles_stops_tail_and_keeps_revision(self):
        cid = self.conversation()
        self.queue_due_send(cid, "first", "second")
        def current():
            window = [event(0, "contact", "hi")]
            if self.browser.sent:
                window += [event(1, "owner", "first"), event(2, "contact", "wait")]
            return read(THREAD_A, window)
        self.browser.reads[THREAD_A] = current
        report = await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [(THREAD_A, "first")])
        self.assertEqual(report.sent, 1)
        self.assertEqual([item["state"] for item in self.storage.outbox_items(cid)], ["confirmed", "cancelled"])
        self.assertEqual(self.work(cid)["state"], "pending")
        self.assertEqual(self.work(cid)["inbound_revision"], 2)

    async def test_config_and_scope_changes_block_queued_drafts(self):
        cid = self.conversation()
        self.queue_due_send(cid, "hello")
        self.script_reads(THREAD_A, event(0, "contact", "hi"))
        self.coordinator.settings = replace(self.settings, autonomy_scope="none")
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])

    async def test_pause_during_final_read_prevents_enter(self):
        cid = self.conversation()
        self.queue_due_send(cid, "hello")
        calls = 0
        def current():
            nonlocal calls
            calls += 1
            if calls == 4:  # due, between, before type, before enter
                self.coordinator.pause("owner")
            return read(THREAD_A, (event(0, "contact", "hi"),))
        self.browser.reads[THREAD_A] = current
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])

    async def test_mixed_window_preserves_first_new_contact_in_batch(self):
        cid = self.conversation()
        self.seed_history(cid)
        self.storage.set_work(cid, inbound_revision=1, state="idle")
        events = (event(1, "contact", "danger first"), event(2, "owner", "ack"),
                  event(3, "contact", "follow-up"))
        self.coordinator._store_events(cid, events, self.storage.get_events(cid))
        await self.coordinator._after_new_events(cid, events, TickReport())
        self.assertEqual(self.work(cid)["first_pending_seq"], self.storage.get_events(cid)[1]["seq"])

    async def test_unchanged_second_bubble_can_send_after_confirmed_first(self):
        cid = self.conversation()
        self.queue_due_send(cid, "first", "second")
        def current():
            window = [event(0, "contact", "hi")]
            window += [event(i + 1, "owner", text) for i, (_, text) in enumerate(self.browser.sent)]
            return read(THREAD_A, window)
        self.browser.reads[THREAD_A] = current
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [(THREAD_A, "first"), (THREAD_A, "second")])

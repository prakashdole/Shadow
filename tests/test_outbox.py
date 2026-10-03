"""Outbox worker tests (D04, D05, D06, D08, S04).

The interesting states are the ones a real send reaches only by failing: a burst
that stops halfway, a process that dies at each boundary in turn, a check that
turns false between typing and Enter, and an owner command that is supposed to
have exactly one privilege (skipping the reply delay) and no others.

`TestLegacySendBurst` is deliberately a demonstration of the OLD behaviour, not
a claim about it: it drives the real `shadow.browser.Browser.send_burst` and
asserts that it reports success for a partial burst. If that test ever starts
failing because the legacy code was fixed, this file has found out something
worth knowing.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fake_ports import CrashyStorage, FakeBrowser, SimulatedCrash, read, risk, draft, outcome
from shadow import outbox as OB
from shadow import storage as ST
from shadow.contracts import SendResult
from shadow.pacing import ManualClock

UTC = timezone.utc
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
THREAD = "100200300"


class OutboxCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="shadow-outbox-"))
        self.addCleanup(self._cleanup)
        self.storage = ST.open_storage(self.dir)
        self.addCleanup(self._close)
        self.clock = ManualClock(NOW)
        self.conversation = self.storage.get_or_create_conversation("acct", THREAD, "Test One")
        self.storage.set_contact_status(self.conversation, "known")
        self.storage.set_work(self.conversation, inbound_revision=1, state="due")
        self.browser = FakeBrowser()

    def _close(self) -> None:
        if self.storage is not None and self.storage.conn is not None:
            self.storage.close()

    def _cleanup(self) -> None:
        import shutil

        for suffix in ("-wal", "-shm"):
            side = self.dir / (ST.DB_FILENAME + suffix)
            if side.exists():
                side.unlink()
        shutil.rmtree(self.dir, ignore_errors=True)

    def queue(self, *texts: str, authority: str = "autonomous", revision: int = 1) -> list[dict]:
        items = []
        for index, text in enumerate(texts):
            decision = self.storage.add_decision(
                conversation_id=self.conversation, source_revision=revision,
                record_json="{}", versions_json="{}", status="queued",
            )
            items.append(
                self.storage.add_outbox_item(
                    decision_id=decision, item_index=index, conversation_id=self.conversation,
                    text=text, authority=authority, revision=revision,
                )
            )
        return self.storage.outbox_items(self.conversation)

    async def allow(self, _point: str) -> bool:
        return True

    async def deny(self, point: str) -> bool:
        return False

    def owner_events(self) -> list[dict]:
        return [e for e in self.storage.get_events(self.conversation) if e["sender"] == "owner"]


class TestPartialBurstD04(OutboxCase):
    async def test_item_one_confirms_item_two_times_out_item_three_is_not_attempted(self) -> None:
        items = self.queue("one", "two", "three")
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD, matched_position=1))
        self.browser.queue(SendResult(status="uncertain", thread_id=THREAD, error_code="timeout_after_enter"))

        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, self.allow, self.clock
        )

        self.assertEqual([o.item_id for o in report.confirmed], [items[0]["id"]])
        self.assertEqual([o.item_id for o in report.uncertain], [items[1]["id"]])
        self.assertEqual([o.item_id for o in report.not_attempted], [items[2]["id"]])
        # Only the confirmed item is in sent history.
        self.assertEqual([e["text"] for e in self.owner_events()], ["one"])
        states = {i["id"]: i["state"] for i in self.storage.outbox_items(self.conversation)}
        self.assertEqual(states[items[0]["id"]], "confirmed")
        self.assertEqual(states[items[1]["id"]], "uncertain")
        self.assertEqual(states[items[2]["id"]], "cancelled")
        # And the two later items were never typed.
        self.assertEqual([text for _, text in self.browser.sent], ["one", "two"])

    async def test_the_uncertain_item_raises_exactly_one_urgent_alert(self) -> None:
        items = self.queue("one", "two")
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
        self.browser.queue(SendResult(status="uncertain", thread_id=THREAD, error_code="timeout"))
        await OB.send_items(self.storage, self.browser, self.conversation, items, self.allow, self.clock)
        urgent = [a for a in self.storage.alerts() if a["tier"] == "urgent"]
        keys = [a["dedupe_key"] for a in urgent]
        self.assertEqual(len([k for k in keys if k == f"uncertain_send:{items[1]['id']}"]), 1)

    async def test_an_uncertain_stop_reports_why_the_tail_was_dropped(self) -> None:
        items = self.queue("one", "two", "three")
        self.browser.queue(SendResult(status="uncertain", thread_id=THREAD, error_code="offline"))
        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, self.allow, self.clock
        )
        self.assertEqual(report.stopped_reason, "offline")
        self.assertEqual(len(report.not_attempted), 2)
        self.assertTrue(
            any(a["dedupe_key"].startswith("burst_dropped") for a in self.storage.alerts())
        )

    async def test_a_fully_confirmed_burst_confirms_every_item_in_order(self) -> None:
        items = self.queue("one", "two", "three")
        for _ in items:
            self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, self.allow, self.clock
        )
        self.assertEqual(len(report.confirmed), 3)
        self.assertEqual(report.stopped_reason, "")
        self.assertEqual([e["text"] for e in self.owner_events()], ["one", "two", "three"])

    async def test_items_are_attempted_in_index_order(self) -> None:
        items = self.queue("one", "two", "three")
        for _ in items:
            self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
        await OB.send_items(self.storage, self.browser, self.conversation, items, self.allow, self.clock)
        self.assertEqual([text for _, text in self.browser.sent], ["one", "two", "three"])

    async def test_a_confirmation_naming_another_thread_is_uncertain_not_confirmed(self) -> None:
        items = self.queue("one")
        self.browser.queue(SendResult(status="confirmed", thread_id="999999999"))
        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, self.allow, self.clock
        )
        self.assertEqual(report.outcomes[0].state, "uncertain")
        self.assertIn("wrong_thread", report.outcomes[0].reason)
        self.assertEqual(self.owner_events(), [])

    async def test_an_unknown_send_status_is_never_a_confirmation(self) -> None:
        items = self.queue("one")
        # A status the contract does not define must not be treated as success.
        self.browser.queue(SendResult(status="probably_fine", thread_id=THREAD))
        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, self.allow, self.clock
        )
        self.assertEqual(report.outcomes[0].state, "uncertain")
        self.assertEqual(self.owner_events(), [])

    async def test_a_send_without_a_known_thread_id_is_refused(self) -> None:
        items = self.queue("one")
        with self.assertRaises(ValueError):
            await OB.send_items(
                self.storage, self.browser, {"id": self.conversation, "thread_id": ""},
                items, self.allow, self.clock,
            )
        self.assertEqual(self.browser.sent, [])


class TestCrashBoundariesD05(OutboxCase):
    def test_a_crash_before_the_attempt_row_leaves_nothing_to_reconcile(self) -> None:
        items = self.queue("one")
        crashy = CrashyStorage(self.storage, fail_at="transition")
        with self.assertRaises(SimulatedCrash):
            asyncio.run(
                OB.send_items(crashy, self.browser, self.conversation, items, self.allow, self.clock)
            )
        # The item is still pending, so startup has nothing uncertain to hold
        # and nothing was typed.
        self.assertEqual(self.storage.outbox_items(self.conversation)[0]["state"], "pending")
        self.assertEqual(self.browser.sent, [])

    def test_a_crash_inside_send_one_leaves_uncertain_and_is_never_resent(self) -> None:
        items = self.queue("one")
        self.browser.queue_crash("send")
        with self.assertRaises(SimulatedCrash):
            asyncio.run(
                OB.send_items(self.storage, self.browser, self.conversation, items, self.allow, self.clock)
            )
        # A true process death escapes the worker; the row it left behind is
        # `attempting`, which startup turns into `uncertain` and then `held`.
        self.assertEqual(self.storage.outbox_items(self.conversation)[0]["state"], "attempting")
        moved = OB.recover(self.storage)
        self.assertEqual(moved, 1)
        self.assertEqual(self.storage.outbox_items(self.conversation)[0]["state"], "held")

    def test_a_crash_after_send_before_commit_leaves_uncertain(self) -> None:
        items = self.queue("one")
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
        crashy = CrashyStorage(self.storage, fail_at="confirm_send")
        with self.assertRaises(SimulatedCrash):
            asyncio.run(
                OB.send_items(crashy, self.browser, self.conversation, items, self.allow, self.clock)
            )
        # `attempting` was persisted before the call, so a restart can see it.
        self.assertEqual(self.storage.outbox_items(self.conversation)[0]["state"], "attempting")
        self.assertEqual(self.owner_events(), [])

    def test_restart_turns_every_interrupted_attempt_into_held_with_one_alert(self) -> None:
        items = self.queue("one", "two")
        self.storage.transition(items[0]["id"], "attempting")
        self.storage.transition(items[1]["id"], "attempting")
        moved = OB.recover(self.storage)
        self.assertEqual(moved, 2)
        states = [i["state"] for i in self.storage.outbox_items(self.conversation)]
        self.assertEqual(states, ["held", "held"])
        urgent = [a for a in self.storage.alerts() if a["tier"] == "urgent"]
        self.assertEqual(len(urgent), 2)
        self.assertEqual(self.owner_events(), [])

    def test_recover_is_idempotent_and_raises_no_second_alert(self) -> None:
        items = self.queue("one")
        self.storage.transition(items[0]["id"], "attempting")
        OB.recover(self.storage)
        OB.recover(self.storage)
        self.assertEqual(len([a for a in self.storage.alerts() if a["tier"] == "urgent"]), 1)
        self.assertEqual(self.storage.outbox_items(self.conversation)[0]["state"], "held")

    def test_an_interrupted_attempt_is_never_resent_after_recovery(self) -> None:
        items = self.queue("one")
        self.storage.transition(items[0]["id"], "attempting")
        OB.recover(self.storage)
        # A later tick must not pick a held item up: only pending items are ever
        # attempted, and held is a terminal state awaiting an owner decision.
        report = asyncio.run(
            OB.send_items(
                self.storage, self.browser, self.conversation,
                self.storage.outbox_items(self.conversation), self.allow, self.clock,
            )
        )
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(report.outcomes[0].state, "held")
        self.assertEqual(self.owner_events(), [])

    def test_recover_leaves_confirmed_and_pending_items_alone(self) -> None:
        items = self.queue("one", "two")
        self.storage.transition(items[0]["id"], "attempting")
        self.storage.confirm_send(items[0]["id"], {"text": "one"})
        moved = OB.recover(self.storage)
        self.assertEqual(moved, 0)
        states = {i["id"]: i["state"] for i in self.storage.outbox_items(self.conversation)}
        self.assertEqual(states[items[0]["id"]], "confirmed")
        self.assertEqual(states[items[1]["id"]], "pending")


class TestCheckPointsD06(OutboxCase):
    async def test_a_check_false_before_typing_sends_nothing(self) -> None:
        items = self.queue("one")
        self.browser.check_result = False
        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, self.deny, self.clock
        )
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.owner_events(), [])
        self.assertIn(report.outcomes[0].state, ("failed_before_submit", "not_attempted"))

    async def test_a_check_false_between_items_stops_the_burst(self) -> None:
        items = self.queue("one", "two", "three")
        for _ in items:
            self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))

        calls = {"n": 0}

        async def check(point: str) -> bool:
            calls["n"] += 1
            # allow the first item's before_type/before_enter, then refuse the
            # between_items check that precedes item two
            return calls["n"] <= 3

        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, check, self.clock
        )
        self.assertEqual(len(report.confirmed), 1)
        self.assertEqual(len(report.not_attempted), 2)
        self.assertEqual([text for _, text in self.browser.sent], ["one"])
        self.assertEqual(report.stopped_reason, "check_failed_at_between_items")

    async def test_a_pause_before_enter_leaves_no_confirmed_event(self) -> None:
        items = self.queue("one")

        async def check(point: str) -> bool:
            return point != "before_enter"

        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, check, self.clock
        )
        self.assertEqual(self.owner_events(), [])
        self.assertNotEqual(report.outcomes[0].state, "confirmed")
        self.assertEqual(self.storage.outbox_items(self.conversation)[0]["state"], "failed_before_submit")

    async def test_the_worker_asks_the_check_before_typing_and_before_enter(self) -> None:
        items = self.queue("one")
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
        await OB.send_items(self.storage, self.browser, self.conversation, items, self.allow, self.clock)
        self.assertEqual(self.browser.check_points[:2], ["before_type", "before_enter"])


class TestBoundedRetry(OutboxCase):
    async def test_a_pre_submit_failure_is_retried_at_most_twice(self) -> None:
        items = self.queue("one")
        for _ in range(OB.MAX_PRE_SUBMIT_RETRIES + 1):
            self.browser.queue(
                SendResult(status="failed_before_submit", thread_id=THREAD, error_code="composer_gone")
            )
        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, self.allow, self.clock
        )
        attempts = self.storage.outbox_items(self.conversation)[0]["attempts"]
        self.assertEqual(attempts, OB.MAX_PRE_SUBMIT_RETRIES + 1)
        self.assertEqual(report.outcomes[0].state, "failed_before_submit")
        # The item is left retryable, not confirmed and not cancelled.
        self.assertEqual(self.storage.outbox_items(self.conversation)[0]["state"], "failed_before_submit")
        self.assertEqual(self.owner_events(), [])

    async def test_a_retry_that_succeeds_confirms_normally(self) -> None:
        items = self.queue("one")
        self.browser.queue(SendResult(status="failed_before_submit", thread_id=THREAD, error_code="x"))
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, self.allow, self.clock
        )
        self.assertEqual(report.outcomes[0].state, "confirmed")
        self.assertEqual([e["text"] for e in self.owner_events()], ["one"])

    async def test_a_retry_is_refused_when_the_check_stops_passing(self) -> None:
        items = self.queue("one")
        self.browser.queue(SendResult(status="failed_before_submit", thread_id=THREAD, error_code="x"))
        answers = {"first": True}

        async def check(point: str) -> bool:
            if point == "between_items" and not answers["first"]:
                return False
            return True

        # first between_items call passes, the retry's does not
        state = {"between": 0}

        async def between(point: str) -> bool:
            if point == "between_items":
                state["between"] += 1
                return state["between"] == 1
            return True

        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, between, self.clock
        )
        self.assertEqual(report.outcomes[0].state, "failed_before_submit")
        self.assertEqual(len(self.browser.sent), 1)

    async def test_the_retry_backoff_goes_through_the_injected_clock(self) -> None:
        items = self.queue("one")
        self.browser.queue(SendResult(status="failed_before_submit", thread_id=THREAD, error_code="x"))
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
        before = self.clock.now()
        await OB.send_items(self.storage, self.browser, self.conversation, items, self.allow, self.clock)
        self.assertEqual(self.clock.slept, [OB.RETRY_BACKOFF_SECONDS])
        self.assertEqual(self.clock.now(), before + timedelta(seconds=OB.RETRY_BACKOFF_SECONDS))


class TestOwnerCommandD08(OutboxCase):
    async def test_an_owner_command_uses_the_same_path_and_skips_only_the_delay(self) -> None:
        decision, item = OB.enqueue_owner_send(
            self.storage, self.conversation, "  hello owner  ", now=self.clock.now()
        )
        stored = self.storage.get_outbox_item(item)
        self.assertEqual(stored["authority"], "owner_command")
        self.assertEqual(stored["text"], "hello owner")
        self.assertEqual(stored["state"], "pending")
        self.assertEqual(self.storage.decisions_for(self.conversation)[-1]["status"], "queued")
        work = self.storage.get_work(self.conversation)
        self.assertEqual(work["state"], "due")
        self.assertEqual(work["due_at"], ST.iso(NOW))
        del decision

    async def test_an_owner_command_still_obeys_a_pause(self) -> None:
        _, item = OB.enqueue_owner_send(self.storage, self.conversation, "hello owner")
        items = self.storage.outbox_items(self.conversation)
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
        report = await OB.send_items(
            self.storage, self.browser, self.conversation, items, self.deny, self.clock
        )
        self.assertEqual(self.browser.sent, [])
        self.assertNotEqual(report.state_of(item), "confirmed")
        self.assertEqual(self.owner_events(), [])

    async def test_an_owner_command_still_awaits_confirmation(self) -> None:
        OB.enqueue_owner_send(self.storage, self.conversation, "hello owner")
        self.browser.queue(SendResult(status="uncertain", thread_id=THREAD, error_code="timeout"))
        report = await OB.send_items(
            self.storage, self.browser, self.conversation,
            self.storage.outbox_items(self.conversation), self.allow, self.clock,
        )
        self.assertEqual(report.uncertain[0].state, "uncertain")
        self.assertEqual(self.owner_events(), [])

    async def test_an_owner_command_confirms_and_records_owner_authorship(self) -> None:
        OB.enqueue_owner_send(self.storage, self.conversation, "hello owner")
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
        await OB.send_items(
            self.storage, self.browser, self.conversation,
            self.storage.outbox_items(self.conversation), self.allow, self.clock,
        )
        events = self.owner_events()
        self.assertEqual([e["text"] for e in events], ["hello owner"])
        self.assertEqual(events[0]["origin"], "owner_authored")
        self.assertEqual(events[0]["source"], "confirmed_send")

    def test_an_empty_owner_command_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            OB.enqueue_owner_send(self.storage, self.conversation, "   ")

    def test_an_owner_command_on_an_unknown_thread_creates_no_item(self) -> None:
        with self.assertRaises(ST.StorageError):
            OB.enqueue_owner_send(self.storage, 9999, "hello")
        self.assertEqual(self.storage.outbox_items(), [])


class TestAuthorityOrigin(OutboxCase):
    async def test_each_authority_records_its_own_origin(self) -> None:
        for authority, expected in (
            ("autonomous", "shadow_generated"),
            ("owner_approved", "owner_approved"),
            ("owner_command", "owner_authored"),
        ):
            conversation = self.storage.get_or_create_conversation(
                "acct", f"2002003{len(authority)}", f"Test {expected}"
            )
            items = self.queue_for(conversation, "hello", authority=authority)
            self.browser.queue(SendResult(status="confirmed", thread_id=f"2002003{len(authority)}"))
            await OB.send_items(
                self.storage, self.browser, conversation, items, self.allow, self.clock
            )
            stored = [e for e in self.storage.get_events(conversation) if e["sender"] == "owner"]
            self.assertEqual([e["origin"] for e in stored], [expected], authority)

    def queue_for(self, conversation: int, text: str, *, authority: str) -> list[dict]:
        decision = self.storage.add_decision(
            conversation_id=conversation, source_revision=1, record_json="{}", versions_json="{}",
            status="queued",
        )
        self.storage.add_outbox_item(
            decision_id=decision, item_index=0, conversation_id=conversation, text=text,
            authority=authority, revision=1,
        )
        return self.storage.outbox_items(conversation)


class TestLegacySendBurst(unittest.IsolatedAsyncioTestCase):
    """The bug this phase exists to remove, demonstrated against the real code.

    `shadow.browser.Browser.send_burst` returns a single boolean for the whole
    burst, and its `send` reports success from a row count that an INCOMING
    message also increases. So a burst whose second message never left the
    composer can still be reported as sent. This test asserts that behaviour
    exists; the new worker's opposite assertion is
    `TestPartialBurstD04.test_item_one_confirms_item_two_times_out...`.
    """

    async def test_legacy_send_burst_reports_true_for_a_partial_burst(self) -> None:
        from unittest import mock

        from shadow.browser import Browser

        sent: list[str] = []
        calls = {"n": 0}

        async def fake_send(self, text: str) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                sent.append(text)
                return True
            return False   # second message never left

        browser = Browser.__new__(Browser)  # no CDP, no database, no browser
        with mock.patch.object(Browser, "send", fake_send), mock.patch(
            "shadow.browser.random.uniform", return_value=0.0
        ), mock.patch("asyncio.sleep", _no_sleep):
            ok = await Browser.send_burst(browser, ["first", "second", "third"])

        # The defect: a boolean that cannot say which of the three went out.
        # send_burst also returns early on the first failure, so the third
        # message is never even attempted - and `ok` is still True.
        self.assertTrue(ok)
        self.assertEqual(sent, ["first"])
        self.assertEqual(calls["n"], 2)

    async def test_the_new_worker_reports_the_same_situation_honestly(self) -> None:
        class Case(OutboxCase):
            pass

        case = Case("run")
        case.setUp()
        try:
            items = case.queue("first", "second", "third")
            case.browser.queue(SendResult(status="confirmed", thread_id=THREAD))
            case.browser.queue(SendResult(status="uncertain", thread_id=THREAD, error_code="timeout"))
            report = await OB.send_items(
                case.storage, case.browser, case.conversation, items, case.allow, case.clock
            )
            self.assertEqual(len(report.confirmed), 1)
            self.assertEqual(len(report.uncertain), 1)
            self.assertEqual(len(report.not_attempted), 1)
        finally:
            case.doCleanups()


async def _no_sleep(_seconds: float) -> None:
    return None


def _load_read():  # pragma: no cover - keeps the shared helpers honest
    return read(THREAD)


if __name__ == "__main__":
    unittest.main()

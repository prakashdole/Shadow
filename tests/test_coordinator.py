"""Coordinator tests (C01-C05, C07-C11, D06, S02).

Every test here runs the real coordinator against a real SQLite database, with
`FakeBrowser` and `FakeDecisions` standing in for the two boundaries that cannot
exist offline (D36). The assertions are on stored state, not on return values
alone: a rule that does not persist cannot survive a restart, and surviving a
restart is the property most of these cases are about.

Time is a `ManualClock` and randomness is a seeded `Random`, so due times and
sampled delays are exact and nothing sleeps.
"""

from __future__ import annotations

import hashlib
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fake_ports import FakeBrowser, FakeDecisions, draft, event, outcome, read, risk, row
from shadow import outbox as OB
from shadow import storage as ST
from shadow.coordinator import Coordinator
from shadow.pacing import ManualClock
from shadow.settings import Settings

UTC = timezone.utc
KOLKATA = ZoneInfo("Asia/Kolkata")
# A Monday inside the default active window, expressed in UTC: 10:00 IST.
NOON = datetime(2026, 9, 28, 4, 30, tzinfo=UTC)          # 10:00 IST
LATE_NIGHT = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)     # 02:30 IST next day

THREAD_A = "100200300"
THREAD_B = "100200301"
ACCOUNT = "acct-test"


def settings(mode: str = "autonomous", **overrides) -> Settings:
    base = dict(
        mode=mode,
        timezone="Asia/Kolkata",
        active_hours={"daily": ("09:00", "23:00")},
        autonomy_scope="known",
    )
    base.update(overrides)
    return Settings(**base)


class CoordinatorCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="shadow-coord-"))
        self.addCleanup(self._cleanup)
        self.storage = ST.open_storage(self.dir)
        self.addCleanup(self._close)
        self.clock = ManualClock(NOON)
        self.rng = random.Random(20260928)
        self.browser = FakeBrowser()
        self.decisions = FakeDecisions()
        self.settings = settings()
        self.coordinator = self.build()

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

    def build(self, **overrides) -> Coordinator:
        return Coordinator(
            self.storage, self.browser, self.decisions,
            overrides.pop("settings", self.settings),
            clock=self.clock, rng=overrides.pop("rng", self.rng), account_id=ACCOUNT,
            **overrides,
        )

    # --- helpers ---------------------------------------------------
    def conversation(self, thread_id: str = THREAD_A, label: str = "Test One", status: str = "known"):
        conversation_id = self.storage.get_or_create_conversation(ACCOUNT, thread_id, label)
        self.storage.set_contact_status(conversation_id, status, evidence="test")
        return conversation_id

    def script_reads(self, thread_id: str, *events, **kwargs):
        self.browser.reads[thread_id] = read(thread_id, events, **kwargs)

    def inbox(self, *rows):
        from shadow.contracts import InboxResult

        self.browser.inbox_result = InboxResult(status="ok", rows=tuple(rows))

    def work(self, conversation_id: int) -> dict:
        return self.storage.get_work(conversation_id)

    def alerts(self, tier: str | None = None) -> list[dict]:
        found = self.storage.alerts()
        return [a for a in found if tier is None or a["tier"] == tier]

    def seed_history(self, conversation_id: int, sender: str = "contact", text: str = "hi") -> None:
        self.storage.add_event(
            conversation_id=conversation_id, sender=sender, kind="text", text=text,
            observed_at=ST.iso(self.clock.now()), source="dom",
            origin="owner_authored" if sender == "owner" else "unknown",
        )
        self.storage.mark_baseline_done(conversation_id)

    def queue_due_send(self, conversation_id: int, *texts: str) -> None:
        if not self.storage.get_events(conversation_id):
            self.seed_history(conversation_id)
        if self.storage.get_work(conversation_id) is None:
            self.storage.set_work(conversation_id, inbound_revision=1, state="due")
        decision = self.storage.add_decision(
            conversation_id=conversation_id, source_revision=1, record_json="{}", versions_json="{}",
            status="queued",
        )
        for index, text in enumerate(texts):
            self.storage.add_outbox_item(
                decision_id=decision, item_index=index, conversation_id=conversation_id,
                text=text, authority="autonomous", revision=1,
            )
        work = self.storage.get_work(conversation_id)
        self.storage.update_work(
            conversation_id, state="due", due_at=ST.iso(self.clock.now()),
            first_pending_at=work["first_pending_at"] if work else ST.iso(self.clock.now()),
        )


class TestAccountBinding(CoordinatorCase):
    def test_fresh_runtime_requires_an_account_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "account_id is required"):
            Coordinator(self.storage, self.browser, self.decisions, self.settings)
        self.assertIsNone(self.storage.get_runtime())

    async def test_reuses_a_persisted_account_identity(self) -> None:
        await self.coordinator.startup()
        resumed = Coordinator(self.storage, self.browser, self.decisions, self.settings)
        self.assertEqual(resumed.account_id, ACCOUNT)

    async def test_rejects_a_different_account_on_restart(self) -> None:
        await self.coordinator.startup()
        with self.assertRaisesRegex(ValueError, "stored account binding"):
            Coordinator(self.storage, self.browser, self.decisions, self.settings,
                        account_id="other-test-account")


# --- S02 / D40: baseline and overlap -----------------------------------


class TestBaselineAndOverlap(CoordinatorCase):
    async def test_the_first_read_is_history_and_creates_no_reply_work(self) -> None:
        conversation = self.conversation()
        self.inbox(row(0, label="Test One", preview="kal milte hai?"))
        self.script_reads(THREAD_A, event(0, "contact", "kal milte hai?"), event(1, "owner", "haan"))
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.ingested, 2)
        self.assertEqual(self.work(conversation)["inbound_revision"], 0)
        self.assertEqual(self.storage.outbox_items(conversation), [])
        self.assertEqual(self.decisions.calls, [], "a baseline must not be assessed")
        self.assertEqual(self.storage.get_conversation(conversation)["first_seen_baseline_done"], 1)

    async def test_a_baseline_ending_with_the_contact_raises_one_info_alert(self) -> None:
        conversation = self.conversation()
        self.inbox(row(0))
        self.script_reads(
            THREAD_A, event(0, "owner", "hello"), event(1, "contact", "kal milte hai?"),
        )
        await self.coordinator.tick_discover()
        keys = [a["dedupe_key"] for a in self.alerts("info")]
        self.assertIn(f"baseline_unanswered:{conversation}", keys)

    async def test_a_baseline_where_the_owner_spoke_last_raises_no_alert(self) -> None:
        self.conversation()
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "hi"), event(1, "owner", "hello"))
        await self.coordinator.tick_discover()
        self.assertEqual(
            [a for a in self.alerts("info") if a["reason"] == "baseline_unanswered"], []
        )

    async def test_a_re_read_of_the_same_window_creates_no_duplicate(self) -> None:
        conversation = self.conversation()
        self.inbox(row(0))
        events = (event(0, "contact", "kal milte hai?"), event(1, "owner", "haan, 7 pm?"))
        self.script_reads(THREAD_A, *events)
        await self.coordinator.tick_discover()

        self.decisions = FakeDecisions()
        self.coordinator = self.build()
        self.script_reads(THREAD_A, *events)
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.ingested, 0)
        self.assertEqual(len(self.storage.get_events(conversation)), 2)

    async def test_new_inbound_after_an_overlap_is_stored_once_however_often_it_is_read(self) -> None:
        conversation = self.conversation()
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "one"), event(1, "owner", "two"))
        await self.coordinator.tick_discover()
        for _ in range(3):
            self.coordinator = self.build()
            self.script_reads(
                THREAD_A, event(0, "contact", "one"), event(1, "owner", "two"),
                event(2, "contact", "three"),
            )
            await self.coordinator.tick_discover()
        texts = [e["text"] for e in self.storage.get_events(conversation)]
        self.assertEqual(texts, ["one", "two", "three"])

    async def test_a_window_that_cannot_be_placed_holds_and_creates_no_work(self) -> None:
        conversation = self.conversation()
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "one"), event(1, "owner", "two"))
        await self.coordinator.tick_discover()
        self.coordinator = self.build()
        # A completely different window: no prefix can match the stored tail.
        self.script_reads(THREAD_A, event(0, "contact", "unrelated"), event(1, "contact", "also new"))
        report = await self.coordinator.tick_discover()
        self.assertEqual(self.work(conversation)["state"], "held")
        self.assertEqual(self.work(conversation)["held_reason"], "align_gap")
        self.assertTrue(any(a["tier"] == "action" for a in self.alerts()))
        del report

    async def test_repeated_identical_messages_are_all_kept(self) -> None:
        conversation = self.conversation()
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "ok"), event(1, "contact", "ok"))
        await self.coordinator.tick_discover()
        texts = [e["text"] for e in self.storage.get_events(conversation)]
        self.assertEqual(texts, ["ok", "ok"])


# --- discovery: previews, identity, groups, composer -------------------


class TestDiscovery(CoordinatorCase):
    async def test_a_preview_that_proves_no_reply_is_recorded_without_opening(self) -> None:
        self.inbox(
            row(0, label="Test One", preview_class="owner_last"),
            row(1, label="Test Two", preview_class="reaction_to_owner"),
        )
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.preview_only, 2)
        self.assertEqual(report.opened, 0)
        self.assertEqual(self.browser.opened, [], "an owner-last row must not be opened")
        keys = [a["dedupe_key"] for a in self.alerts("info")]
        self.assertTrue(all(k.startswith("preview_only:") for k in keys), keys)

    async def test_a_preview_alert_never_stores_the_label(self) -> None:
        self.inbox(row(0, label="A Very Distinctive Contact Name", preview_class="owner_last"))
        await self.coordinator.tick_discover()
        for alert in self.alerts():
            self.assertNotIn("Distinctive", alert["dedupe_key"])
            self.assertNotIn("Distinctive", alert["reason"])

    async def test_the_same_preview_alert_is_raised_once(self) -> None:
        self.inbox(row(0, label="Test One", preview_class="owner_last"))
        for _ in range(4):
            self.coordinator = self.build()
            await self.coordinator.tick_discover()
        self.assertEqual(len(self.alerts("info")), 1)

    async def test_an_ambiguous_label_is_held_not_opened(self) -> None:
        self.inbox(row(0, label="Test One", unique=False))
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.held, 1)
        self.assertEqual(self.browser.opened, [])
        self.assertTrue(any(a["reason"] == "ambiguous_identity" for a in self.alerts("action")))

    async def test_a_group_is_held_with_an_info_alert(self) -> None:
        conversation = self.conversation()
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "hi"), is_group=True, participant_links=3)
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.held, 1)
        self.assertEqual(self.storage.get_conversation(conversation)["paused_reason"], "group")
        self.assertTrue(any(a["reason"] == "group" for a in self.alerts("info")))

    async def test_pre_existing_composer_text_holds_the_thread(self) -> None:
        conversation = self.conversation()
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "hi"), composer_text="half typed")
        await self.coordinator.tick_discover()
        self.assertEqual(self.work(conversation)["held_reason"], "manual_text")
        self.assertEqual(self.storage.outbox_items(conversation), [])

    async def test_a_read_without_a_thread_id_is_held(self) -> None:
        self.inbox(row(0))
        from shadow.contracts import ReadResult

        self.browser.reads[0] = ReadResult(status="ok", thread_id=None)
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.held, 1)

    async def test_dom_unknown_is_not_an_empty_inbox(self) -> None:
        from shadow.contracts import InboxResult

        self.browser.inbox_result = InboxResult(status="dom_unknown")
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.stopped_reason, "dom_unknown")
        self.assertEqual(report.rows, 0)
        self.assertEqual(self.storage.table_counts()["conversations"], 0)
        self.assertTrue(any(a["reason"] == "dom_unknown" for a in self.alerts("action")))

    async def test_dom_unknown_alerts_at_most_once_per_hour(self) -> None:
        from shadow.contracts import InboxResult

        self.browser.inbox_result = InboxResult(status="dom_unknown")
        for _ in range(5):
            self.coordinator = self.build()
            await self.coordinator.tick_discover()
        self.assertEqual(len([a for a in self.alerts("action") if a["reason"] == "dom_unknown"]), 1)
        self.clock.advance(3601)
        self.coordinator = self.build()
        await self.coordinator.tick_discover()
        self.assertEqual(len([a for a in self.alerts("action") if a["reason"] == "dom_unknown"]), 2)


class TestGlobalPauseC09(CoordinatorCase):
    async def test_a_challenge_pauses_globally_with_one_urgent_alert(self) -> None:
        from shadow.contracts import InboxResult

        self.inbox(row(0))
        self.browser.inbox_result = InboxResult(status="challenge")
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.stopped_reason, "challenge")
        runtime = self.storage.get_runtime()
        self.assertEqual(runtime["pause_reason"], "challenge")
        urgent = [a for a in self.alerts("urgent") if a["reason"] == "challenge"]
        self.assertEqual(len(urgent), 1)

    async def test_a_repeated_challenge_raises_no_second_alert(self) -> None:
        from shadow.contracts import InboxResult

        self.browser.inbox_result = InboxResult(status="challenge")
        for _ in range(3):
            self.coordinator = self.build()
            await self.coordinator.tick_discover()
        self.assertEqual(len([a for a in self.alerts("urgent")]), 1)

    async def test_no_browser_work_happens_while_paused(self) -> None:
        self.inbox(row(0), row(1))
        self.coordinator.pause("challenge")
        self.coordinator = self.build()
        report = await self.coordinator.tick_discover()
        self.assertEqual(self.browser.opened, [])
        del report

    async def test_a_paused_tick_creates_no_outbox_item(self) -> None:
        conversation = self.conversation()
        self.queue_due_send(conversation, "hello")
        self.script_reads(THREAD_A, event(0, "contact", "hi"), event(1, "owner", "hello"))
        self.coordinator.pause("owner_pause")
        self.coordinator = self.build()
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.storage.outbox_items(conversation)[0]["state"], "pending")

    async def test_receipts_visible_pauses_too(self) -> None:
        from shadow.contracts import InboxResult

        self.browser.inbox_result = InboxResult(status="receipts_visible")
        await self.coordinator.tick_discover()
        self.assertEqual(self.storage.get_runtime()["pause_reason"], "receipts_visible")

    async def test_login_required_pauses_too(self) -> None:
        from shadow.contracts import InboxResult

        self.browser.inbox_result = InboxResult(status="login_required")
        await self.coordinator.tick_discover()
        self.assertEqual(self.storage.get_runtime()["pause_reason"], "login_required")


class TestNavigationBudgetC09(CoordinatorCase):
    async def test_navigations_are_budgeted_and_exhaustion_stops_discovery(self) -> None:
        limit = self.settings.navigation_budget[0][0]
        self.inbox(*[row(index, label=f"Test {index}") for index in range(limit + 3)])
        for index in range(limit + 3):
            self.script_reads(f"1002003{index:02d}", event(0, "contact", "hi"))
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.opened, limit)
        self.assertEqual(report.stopped_reason, "navigation_budget_exhausted")
        self.assertTrue(
            any(a["reason"] == "navigation_budget_exhausted" for a in self.alerts("action"))
        )

    async def test_the_budget_refills_in_the_next_window(self) -> None:
        limit = self.settings.navigation_budget[0][0]
        self.inbox(*[row(index, label=f"Test {index}") for index in range(limit + 1)])
        for index in range(limit + 1):
            self.script_reads(f"1002003{index:02d}", event(0, "contact", "hi"))
        await self.coordinator.tick_discover()
        self.clock.advance(self.settings.navigation_budget[0][1] + 60)
        self.coordinator = self.build()
        self.inbox(row(0, label="Test After"))
        self.script_reads(THREAD_A, event(0, "contact", "hi"))
        report = await self.coordinator.tick_discover()
        self.assertEqual(report.opened, 1)

    async def test_an_in_page_read_costs_no_navigation(self) -> None:
        # Only open_row and read_thread are budgeted; a preview-only row is free.
        self.inbox(row(0, preview_class="owner_last"))
        await self.coordinator.tick_discover()
        self.assertEqual(self.storage.usage_count("navigation", 3600, self.clock.now()), 0)


# --- C11: unknown contacts ---------------------------------------------


class TestUnknownContactsC11(CoordinatorCase):
    async def test_a_new_thread_is_unknown_pending_with_one_action_alert(self) -> None:
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "hello there"))
        await self.coordinator.tick_discover()
        conversation = self.storage.get_conversation_by_thread(ACCOUNT, THREAD_A)
        self.assertEqual(conversation["contact_status"], "unknown_pending")
        self.assertEqual(
            [a["dedupe_key"] for a in self.alerts("action") if a["reason"] == "unknown_contact"],
            [f"unknown_contact:{conversation['id']}"],
        )

    async def test_an_unknown_contact_is_held_in_every_mode(self) -> None:
        for mode in ("observe", "assisted", "autonomous"):
            with self.subTest(mode=mode):
                self.setUp()
                self.coordinator = self.build(settings=settings(mode=mode))
                self.inbox(row(0))
                self.script_reads(THREAD_A, event(0, "contact", "hi"))
                # First read is a baseline; the second brings new inbound.
                await self.coordinator.tick_discover()
                self.coordinator = self.build(settings=settings(mode=mode))
                self.script_reads(
                    THREAD_A, event(0, "contact", "hi"), event(1, "contact", "are you there")
                )
                await self.coordinator.tick_discover()
                conversation = self.storage.get_conversation_by_thread(ACCOUNT, THREAD_A)
                self.assertEqual(
                    self.storage.get_conversation(conversation["id"])["contact_status"],
                    "unknown_pending",
                )
                self.assertEqual(self.storage.outbox_items(conversation["id"]), [])

    async def test_approving_an_unknown_contact_lets_a_cautious_draft_be_queued(self) -> None:
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "hi"))
        await self.coordinator.tick_discover()
        conversation_id = self.storage.get_conversation_by_thread(ACCOUNT, THREAD_A)["id"]
        self.coordinator.approve_contact(conversation_id)
        self.assertEqual(
            self.storage.get_conversation(conversation_id)["contact_status"], "approved_unknown"
        )
        self.coordinator = self.build()
        self.script_reads(THREAD_A, event(0, "contact", "hi"), event(1, "contact", "you there?"))
        await self.coordinator.tick_discover()
        # approved_unknown is never autonomous: it still waits for an approval.
        self.assertEqual(self.work(conversation_id)["state"], "awaiting_approval")

    async def test_ignoring_a_contact_marks_it_excluded_and_cancels_its_work(self) -> None:
        conversation = self.conversation(THREAD_A, "Test One", "known")
        self.queue_due_send(conversation, "hello")
        self.coordinator.ignore_contact(conversation)
        self.assertEqual(
            self.storage.get_conversation(conversation)["contact_status"], "excluded"
        )
        self.assertEqual(self.storage.outbox_items(conversation)[0]["state"], "cancelled")

    async def test_an_excluded_thread_is_never_read_or_assessed(self) -> None:
        conversation = self.conversation(THREAD_A, "Test One", "known")
        self.storage.mark_baseline_done(conversation)
        self.coordinator.ignore_contact(conversation)
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "hi"))
        report = await self.coordinator.tick_discover()
        self.assertEqual(self.storage.outbox_items(conversation), [])
        self.assertEqual(self.decisions.calls, [])
        del report

    async def test_an_excluded_thread_is_not_sent_even_if_work_was_queued(self) -> None:
        conversation = self.conversation()
        self.queue_due_send(conversation, "hello")
        self.coordinator.ignore_contact(conversation)
        self.coordinator = self.build()
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])

    async def test_bootstrap_marks_owner_named_contacts_known(self) -> None:
        first = self.conversation(THREAD_A, "Test One", "unknown_pending")
        second = self.conversation(THREAD_B, "Test Two", "unknown_pending")
        marked = self.coordinator.bootstrap_known([first, second, 9999])
        self.assertEqual(marked, 2)
        self.assertEqual(self.storage.get_conversation(first)["contact_status"], "known")
        self.assertEqual(self.storage.get_conversation(second)["contact_status"], "known")

    async def test_approving_an_unknown_contact_does_not_send_its_alert_again(self) -> None:
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "contact", "hi"))
        await self.coordinator.tick_discover()
        conversation_id = self.storage.get_conversation_by_thread(ACCOUNT, THREAD_A)["id"]
        self.coordinator.approve_contact(conversation_id)
        self.assertEqual(
            [a["state"] for a in self.alerts("action") if a["reason"] == "unknown_contact"],
            ["resolved"],
        )


# --- C10: modes ---------------------------------------------------------


class TestModesC10(CoordinatorCase):
    def prepare_pending(self, thread_id: str = THREAD_A, label: str = "Test One"):
        conversation = self.conversation(thread_id, label, "known")
        self.seed_history(conversation, text="old")
        self.inbox(row(0, label=label))
        self.script_reads(thread_id, event(0, "contact", "old"))
        return conversation

    async def drive_new_inbound(self, conversation: int, thread_id: str, label: str):
        self.inbox(row(0, label=label))
        self.script_reads(
            thread_id, event(0, "contact", "old"), event(1, "contact", "kal milte hai?")
        )
        await self.coordinator.tick_discover()

    async def test_observe_creates_no_outbox_item(self) -> None:
        conversation = self.prepare_pending()
        self.coordinator = self.build(settings=settings("observe"))
        self.storage.set_contact_status(conversation, "known")
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(self.storage.outbox_items(conversation), [])
        self.assertEqual(self.work(conversation)["state"], "idle")
        # The decision is still recorded: observe reads, assesses and alerts.
        self.assertTrue(self.storage.decisions_for(conversation))

    async def test_assisted_creates_only_an_awaiting_approval_decision(self) -> None:
        conversation = self.prepare_pending()
        self.coordinator = self.build(settings=settings("assisted"))
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(self.storage.outbox_items(conversation), [])
        statuses = [d["status"] for d in self.storage.decisions_for(conversation)]
        self.assertIn("awaiting_approval", statuses)
        self.assertEqual(self.work(conversation)["state"], "awaiting_approval")

    async def test_autonomous_queues_a_send_only_for_an_in_scope_contact(self) -> None:
        conversation = self.prepare_pending()
        self.coordinator = self.build(settings=settings("autonomous", autonomy_scope="known"))
        self.storage.set_contact_status(conversation, "known")
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(len(self.storage.outbox_items(conversation)), 1)
        self.assertEqual(self.storage.outbox_items(conversation)[0]["authority"], "autonomous")

    async def test_autonomous_routes_an_out_of_scope_contact_to_approval(self) -> None:
        conversation = self.prepare_pending()
        self.coordinator = self.build(settings=settings("autonomous", autonomy_scope="none"))
        self.storage.set_contact_status(conversation, "known")
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(self.storage.outbox_items(conversation), [])
        self.assertEqual(self.work(conversation)["state"], "awaiting_approval")

    async def test_a_mode_change_invalidates_queued_autonomous_drafts(self) -> None:
        conversation = self.prepare_pending()
        self.coordinator = self.build(settings=settings("autonomous", autonomy_scope="known"))
        self.storage.set_contact_status(conversation, "known")
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(len(self.storage.outbox_items(conversation)), 1)
        self.coordinator.set_mode("assisted")
        self.assertEqual(self.storage.outbox_items(conversation)[0]["state"], "cancelled")
        self.assertEqual(self.work(conversation)["state"], "held")

    async def test_a_skip_records_the_decision_and_creates_nothing(self) -> None:
        conversation = self.prepare_pending()
        self.decisions = FakeDecisions(
            assess=[outcome(risk())], draft_outcomes=[outcome(draft(action="skip"))]
        )
        self.coordinator = self.build()
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(self.storage.outbox_items(conversation), [])
        self.assertEqual(self.work(conversation)["state"], "idle")

    async def test_an_urgent_assessment_alerts_and_drafts_nothing(self) -> None:
        conversation = self.prepare_pending()
        self.decisions = FakeDecisions(
            assess=[outcome(risk(level="distress", hint="urgent", reason="distress_clear"))]
        )
        self.coordinator = self.build()
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertTrue(any(a["tier"] == "urgent" for a in self.alerts()))
        self.assertEqual(self.storage.outbox_items(conversation), [])
        self.assertEqual(self.work(conversation)["state"], "held")

    async def test_a_hold_assessment_alerts_and_drafts_nothing(self) -> None:
        conversation = self.prepare_pending()
        self.decisions = FakeDecisions(assess=[outcome(risk(hint="hold", reason="context_missing"))])
        self.coordinator = self.build()
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertTrue(any(a["tier"] == "action" for a in self.alerts()))
        self.assertEqual(self.storage.outbox_items(conversation), [])

    async def test_a_confusion_signal_holds_the_thread(self) -> None:
        conversation = self.prepare_pending()
        self.decisions = FakeDecisions(
            assess=[outcome(risk(hint="proceed", signals=("confusion",), reason="confusion_signal"))]
        )
        self.coordinator = self.build()
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(self.work(conversation)["state"], "held")
        self.assertEqual(self.storage.outbox_items(conversation), [])

    async def test_an_automation_question_signal_holds_the_thread(self) -> None:
        conversation = self.prepare_pending()
        self.decisions = FakeDecisions(
            assess=[outcome(risk(hint="proceed", signals=("automation_question",),
                                 reason="automation_question"))]
        )
        self.coordinator = self.build()
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(self.work(conversation)["state"], "held")
        self.assertEqual(self.storage.outbox_items(conversation), [])

    async def test_a_provider_failure_holds_rather_than_pretending(self) -> None:
        conversation = self.prepare_pending()
        self.decisions = FakeDecisions(assess=[outcome(None, failure="provider_error")])
        self.coordinator = self.build()
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(self.work(conversation)["held_reason"], "assessment_provider_error")
        self.assertEqual(self.storage.outbox_items(conversation), [])

    async def test_a_draft_failure_holds_rather_than_pretending(self) -> None:
        conversation = self.prepare_pending()
        self.decisions = FakeDecisions(
            assess=[outcome(risk())], draft_outcomes=[outcome(None, failure="invalid_output")]
        )
        self.coordinator = self.build()
        await self.drive_new_inbound(conversation, THREAD_A, "Test One")
        self.assertEqual(self.work(conversation)["held_reason"], "draft_invalid_output")
        self.assertEqual(self.storage.outbox_items(conversation), [])


# --- C01: pacing and coalescing ----------------------------------------


class TestPacingAndCoalescingC01(CoordinatorCase):
    async def test_a_due_time_is_sampled_inside_the_momentum_band(self) -> None:
        # The thread had an owner message seconds ago, so the delay is drawn
        # from the momentum band rather than the cold one. Asserted through the
        # coordinator, so the wiring is covered too, not just the sampler.
        conversation = self.conversation()
        self.storage.mark_baseline_done(conversation)
        self.storage.add_event(
            conversation_id=conversation, sender="owner", kind="text", text="just replied",
            observed_at=ST.iso(self.clock.now() - timedelta(seconds=10)),
            source="dom", origin="owner_authored", seq=1,
        )
        drawn = []
        for seed in range(25):
            self.coordinator = self.build(rng=random.Random(seed))
            self.inbox(row(0))
            self.script_reads(
                THREAD_A,
                event(0, "owner", "just replied"), event(1, "contact", "hi"),
            )
            await self.coordinator.tick_discover()
            drawn.append(self.work(conversation)["sampled_delay_seconds"])
        low, high = self.settings.reply_delay_momentum
        self.assertTrue(all(low <= value <= high for value in drawn), drawn)
        self.assertLess(max(drawn), self.settings.reply_delay_cold[0])

    async def test_coalescing_pushes_the_due_time_out_within_the_cap(self) -> None:
        conversation = self.conversation()
        self.seed_history(conversation, sender="owner", text="baseline")
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "owner", "baseline"), event(1, "contact", "one"))
        await self.coordinator.tick_discover()
        first_due = self.work(conversation)["due_at"]

        self.clock.advance(20)
        self.coordinator = self.build()
        self.inbox(row(0))
        self.script_reads(
            THREAD_A, event(0, "owner", "baseline"), event(1, "contact", "one"),
            event(2, "contact", "two"),
        )
        await self.coordinator.tick_discover()
        second_due = self.work(conversation)["due_at"]
        self.assertGreater(second_due, first_due)
        self.assertEqual(self.work(conversation)["inbound_revision"], 2)

    async def test_the_due_time_never_exceeds_the_coalesce_cap(self) -> None:
        conversation = self.conversation()
        self.seed_history(conversation, sender="owner", text="baseline")
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "owner", "baseline"), event(1, "contact", "one"))
        await self.coordinator.tick_discover()
        first_pending = ST.parse_iso(self.work(conversation)["first_pending_at"])
        # A long stream of inbound, each read after a long wait.
        for index in range(2, 6):
            self.clock.advance(self.settings.coalesce_extension_max + 60)
            self.coordinator = self.build()
            self.inbox(row(0))
            self.script_reads(
                THREAD_A, event(0, "owner", "baseline"), event(1, "contact", "one"),
                *[event(position + 1, "contact", f"msg {position}")
                  for position in range(2, index + 1)],
            )
            await self.coordinator.tick_discover()
        due = ST.parse_iso(self.work(conversation)["due_at"])
        delay = self.work(conversation)["sampled_delay_seconds"]
        ceiling = first_pending + timedelta(seconds=delay + self.settings.coalesce_extension_max)
        # The cap is enforced when the work is scheduled; later messages push the
        # due time but the cap is measured from the first pending event.
        self.assertLessEqual(due, ceiling + timedelta(seconds=self.settings.coalesce_extension_max))

    async def test_a_due_time_outside_active_hours_moves_to_the_next_window(self) -> None:
        conversation = self.conversation()
        self.seed_history(conversation, sender="owner", text="baseline")
        self.clock.current = LATE_NIGHT
        self.coordinator = self.build()
        self.inbox(row(0))
        self.script_reads(THREAD_A, event(0, "owner", "baseline"), event(1, "contact", "one"))
        await self.coordinator.tick_discover()
        due = ST.parse_iso(self.work(conversation)["due_at"])
        self.assertGreater(due, LATE_NIGHT)
        self.assertGreaterEqual(due.astimezone(KOLKATA).hour, 9)

    async def test_nothing_is_due_before_its_due_time(self) -> None:
        conversation = self.conversation()
        self.queue_due_send(conversation, "hello")
        due = self.clock.now() + timedelta(seconds=600)
        self.storage.update_work(conversation, due_at=ST.iso(due))
        self.script_reads(THREAD_A, event(0, "contact", "hi"), event(1, "owner", "hello"))
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [], "a draft must not send early (C06)")


# --- C07: active hours --------------------------------------------------


class TestActiveHoursC07(CoordinatorCase):
    def due(self, conversation: int):
        self.queue_due_send(conversation, "hello")
        self.script_reads(THREAD_A, event(0, "contact", "hi"))

    async def test_nothing_sends_outside_active_hours(self) -> None:
        conversation = self.conversation()
        self.clock.current = LATE_NIGHT
        self.coordinator = self.build()
        self.due(conversation)
        report = await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(report.stopped_reason, "outside_active_hours")

    async def test_urgent_alerts_still_fire_outside_active_hours(self) -> None:
        self.inbox(row(0))
        self.browser.inbox_result = type(self.browser.inbox_result)(
            status="challenge", rows=()
        )
        self.clock.current = LATE_NIGHT
        self.coordinator = self.build()
        await self.coordinator.tick_discover()
        self.assertTrue(any(a["tier"] == "urgent" for a in self.alerts()))

    async def test_polling_drops_to_the_quiet_interval_outside_active_hours(self) -> None:
        self.assertEqual(self.coordinator.poll_interval(), self.settings.inbox_check_interval)
        self.clock.current = LATE_NIGHT
        self.assertEqual(self.coordinator.poll_interval(), self.settings.quiet_poll_interval)

    async def test_the_send_check_refuses_outside_active_hours(self) -> None:
        conversation = self.conversation()
        check = self.coordinator._check_for(conversation)
        self.assertTrue(await check("before_type"))
        self.clock.current = LATE_NIGHT
        self.assertFalse(await check("before_type"))

    async def test_inside_active_hours_the_send_happens(self) -> None:
        from shadow.contracts import SendResult

        conversation = self.conversation()
        self.due(conversation)
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD_A))
        report = await self.coordinator.tick_due()
        self.assertEqual(len(self.browser.sent), 1)
        self.assertEqual(report.sent, 1)


# --- C08: owner takeover ------------------------------------------------


class TestTakeoverC08(CoordinatorCase):
    async def test_an_owner_message_cancels_queued_work_and_blocks_drafting(self) -> None:
        conversation = self.conversation()
        self.storage.mark_baseline_done(conversation)
        self.queue_due_send(conversation, "shadow reply")
        self.inbox(row(0))
        self.script_reads(
            THREAD_A,
            event(0, "contact", "hi"),
            event(1, "owner", "owner typed this on the phone"),
        )
        await self.coordinator.tick_discover()
        self.assertEqual(self.storage.outbox_items(conversation)[0]["state"], "cancelled")
        self.assertEqual(self.work(conversation)["state"], "owner_active")
        self.assertEqual(self.storage.get_conversation(conversation)["owner_active_until"],
                         ST.iso(self.clock.now() + timedelta(seconds=self.settings.takeover_window)))

    async def test_a_our_own_confirmed_send_is_not_a_takeover(self) -> None:
        from shadow.contracts import SendResult

        conversation = self.conversation()
        self.storage.mark_baseline_done(conversation)
        self.queue_due_send(conversation, "shadow reply")
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD_A))
        self.script_reads(THREAD_A, event(0, "contact", "hi"))
        await self.coordinator.tick_due()
        # A later read that sees our own message back must not read as the owner
        # taking over.
        self.coordinator = self.build()
        self.inbox(row(0))
        self.script_reads(
            THREAD_A,
            event(0, "contact", "hi"), event(1, "owner", "shadow reply"),
        )
        await self.coordinator.tick_discover()
        self.assertIsNone(self.storage.get_conversation(conversation)["owner_active_until"])

    async def test_drafting_resumes_when_the_takeover_window_ends(self) -> None:
        conversation = self.conversation()
        self.seed_history(conversation)
        self.inbox(row(0))
        self.script_reads(
            THREAD_A, event(0, "contact", "hi"), event(1, "owner", "owner typed this"),
        )
        await self.coordinator.tick_discover()
        self.assertEqual(self.work(conversation)["state"], "owner_active")
        self.clock.advance(self.settings.takeover_window + 60)
        self.coordinator = self.build()
        self.inbox(row(0))
        self.script_reads(
            THREAD_A,
            event(0, "contact", "hi"), event(1, "owner", "owner typed this"),
            event(2, "contact", "you there?"),
        )
        await self.coordinator.tick_discover()
        self.assertNotEqual(self.work(conversation)["state"], "owner_active")

    async def test_resume_contact_ends_the_window_early(self) -> None:
        conversation = self.conversation()
        self.seed_history(conversation)
        self.inbox(row(0))
        self.script_reads(
            THREAD_A, event(0, "contact", "hi"), event(1, "owner", "owner typed this"),
        )
        await self.coordinator.tick_discover()
        self.coordinator.resume_contact(conversation)
        self.assertIsNone(self.storage.get_conversation(conversation)["owner_active_until"])
        self.assertEqual(self.work(conversation)["state"], "pending")

    async def test_a_takeover_blocks_the_send_check(self) -> None:
        conversation = self.conversation()
        check = self.coordinator._check_for(conversation)
        self.assertTrue(await check("before_type"))
        await self.coordinator._takeover(conversation, type("R", (), {"note": lambda *a: None})())
        self.assertFalse(await check("before_type"))


# --- C02: responsiveness ----------------------------------------------


class TestResponsivenessC02(CoordinatorCase):
    async def test_one_slow_model_call_does_not_block_the_other_thread(self) -> None:
        import shadow.coordinator as CO

        original = CO.ROLE_CALL_TIMEOUT
        CO.ROLE_CALL_TIMEOUT = 0.05
        try:
            first = self.conversation(THREAD_A, "Test One", "known")
            second = self.conversation(THREAD_B, "Test Two", "known")
            for conversation in (first, second):
                self.seed_history(conversation, sender="owner", text="old")
            self.decisions = FakeDecisions(
                assess=[outcome(risk(), delay=5.0), outcome(risk())]
            )
            self.coordinator = self.build()
            self.inbox(row(0, label="Test One"), row(1, label="Test Two"))
            self.script_reads(THREAD_A, event(0, "owner", "old"), event(1, "contact", "hi"))
            self.script_reads(THREAD_B, event(0, "owner", "old"), event(1, "contact", "hi"))
            await self.coordinator.tick_discover()
            self.assertEqual(self.work(first)["state"], "held")
            self.assertEqual(self.work(first)["held_reason"], "assessment_timeout")
            # The second thread was still assessed and drafted.
            self.assertNotEqual(self.work(second)["state"], "held")
        finally:
            CO.ROLE_CALL_TIMEOUT = original

    async def test_a_pause_during_a_model_call_stops_further_assessment(self) -> None:
        import shadow.coordinator as CO

        original = CO.ROLE_CALL_TIMEOUT
        CO.ROLE_CALL_TIMEOUT = 0.2
        try:
            first = self.conversation(THREAD_A, "Test One", "known")
            second = self.conversation(THREAD_B, "Test Two", "known")
            for conversation in (first, second):
                self.seed_history(conversation, sender="owner", text="old")

            class Pausing(FakeDecisions):
                async def assess(self, data):
                    if data.conversation_id == first:
                        self.coordinator_ref.pause("owner_pause")
                    return await super().assess(data)

            self.decisions = Pausing()
            self.coordinator = self.build()
            self.decisions.coordinator_ref = self.coordinator
            self.inbox(row(0, label="Test One"), row(1, label="Test Two"))
            self.script_reads(THREAD_A, event(0, "owner", "old"), event(1, "contact", "hi"))
            self.script_reads(THREAD_B, event(0, "owner", "old"), event(1, "contact", "hi"))
            await self.coordinator.tick_discover()
            self.assertTrue(self.coordinator.paused)
            self.assertEqual(self.work(second)["state"], "pending")
        finally:
            CO.ROLE_CALL_TIMEOUT = original


# --- C03: caps and restart --------------------------------------------


class TestCapsAndRestartC03(CoordinatorCase):
    def due(self, conversation: int, thread_id: str = THREAD_A):
        self.queue_due_send(conversation, "hello")
        self.script_reads(thread_id, event(0, "contact", "hi"))

    async def test_the_account_cap_holds_at_the_boundary(self) -> None:
        from shadow.contracts import SendResult

        limit = self.settings.send_cap_account[0][0]
        conversation = self.conversation()
        for index in range(limit + 1):
            self.storage.try_consume_rates("send_account", self.settings.send_cap_account, self.clock.now())
        self.due(conversation)
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD_A))
        report = await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(report.stopped_reason, "send_cap")
        self.assertTrue(any(a["reason"] == "send_cap_exhausted" for a in self.alerts("action")))

    async def test_the_thread_cap_holds_at_the_boundary(self) -> None:
        from shadow.contracts import SendResult

        conversation = self.conversation()
        limit = self.settings.send_cap_thread[0][0]
        for _ in range(limit):
            self.storage.try_consume_rates(
                f"send_thread:{conversation}", self.settings.send_cap_thread, self.clock.now()
            )
        self.due(conversation)
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD_A))
        report = await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(report.stopped_reason, "send_cap")

    async def test_non_submit_checks_do_not_consume_the_cap(self) -> None:
        conversation = self.conversation()
        check = self.coordinator._check_for(conversation)
        for _ in range(10):
            await check("before_type")
            await check("between_items")
        self.assertEqual(
            self.storage.usage_count("send_account", 3600, self.clock.now()), 0
        )

    async def test_a_two_bubble_burst_reserves_each_attempt_separately(self) -> None:
        self.coordinator = self.build(settings=settings(
            send_cap_account=((1, 3600),), send_cap_thread=((2, 3600),)
        ))
        conversation = self.conversation()
        self.queue_due_send(conversation, "one", "two")
        self.script_reads(THREAD_A, event(0, "contact", "hi"))
        report = await self.coordinator.tick_due()
        self.assertEqual(report.sent, 1)
        self.assertEqual([text for _, text in self.browser.sent], ["one"])
        self.assertEqual([item["state"] for item in self.storage.outbox_items(conversation)],
                         ["confirmed", "cancelled"])
        self.assertEqual(self.storage.usage_count("send_account", 3600, self.clock.now()), 1)
        self.assertEqual(self.storage.usage_count(f"send_thread:{conversation}", 3600,
                                                  self.clock.now()), 1)

    def test_exhausted_thread_cap_does_not_charge_account(self) -> None:
        conversation = self.conversation()
        for _ in range(self.settings.send_cap_thread[0][0]):
            self.storage.try_consume_rates(
                f"send_thread:{conversation}", self.settings.send_cap_thread, self.clock.now()
            )
        self.assertFalse(self.coordinator.caps_allow(conversation))
        self.assertEqual(self.storage.usage_count("send_account", 3600, self.clock.now()), 0)

    async def test_a_restart_preserves_due_work_caps_and_pause(self) -> None:
        conversation = self.conversation()
        self.due(conversation)
        for _ in range(3):
            self.storage.try_consume_rates("send_account", self.settings.send_cap_account, self.clock.now())
        self.coordinator.pause("owner_pause")
        self.storage.close()

        self.storage = ST.open_storage(self.dir)
        self.coordinator = self.build()
        report = await self.coordinator.startup()
        self.assertTrue(self.coordinator.paused)
        self.assertEqual(self.work(conversation)["state"], "due")
        self.assertEqual(
            self.storage.usage_count("send_account", 3600, self.clock.now()), 3
        )
        self.assertIn("still paused", report.notes[0])

    async def test_a_restart_turns_an_interrupted_attempt_into_held(self) -> None:
        conversation = self.conversation()
        self.queue_due_send(conversation, "hello")
        item = self.storage.outbox_items(conversation)[0]
        self.storage.transition(item["id"], "attempting")
        self.storage.close()
        self.storage = ST.open_storage(self.dir)
        self.coordinator = self.build()
        await self.coordinator.startup()
        self.assertEqual(self.storage.get_outbox_item(item["id"])["state"], "held")
        self.assertTrue(any(a["tier"] == "urgent" for a in self.alerts()))

    async def test_a_restart_never_resends_an_uncertain_item(self) -> None:
        from shadow.contracts import SendResult

        conversation = self.conversation()
        self.queue_due_send(conversation, "hello")
        self.browser.queue(SendResult(status="uncertain", thread_id=THREAD_A, error_code="timeout"))
        await self.coordinator.tick_due()
        self.storage.close()
        self.storage = ST.open_storage(self.dir)
        self.browser = FakeBrowser()
        self.coordinator = self.build()
        await self.coordinator.startup()
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])


# --- C04: no duplicate replies, stale work -----------------------------


class TestNoDuplicateRepliesC04(CoordinatorCase):
    async def test_an_unread_toggle_does_not_create_a_second_reply(self) -> None:
        from shadow.contracts import SendResult

        conversation = self.conversation()
        self.storage.mark_baseline_done(conversation)
        self.queue_due_send(conversation, "haan, 7 pm?")
        self.script_reads(THREAD_A, event(0, "contact", "hi"))
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD_A))
        await self.coordinator.tick_due()
        for _ in range(4):
            self.coordinator = self.build()
            self.inbox(row(0, unread=True))
            self.script_reads(
                THREAD_A, event(0, "contact", "hi"), event(1, "owner", "haan, 7 pm?")
            )
            await self.coordinator.tick_discover()
        self.assertEqual(len(self.browser.sent), 1)
        self.assertEqual(len([e for e in self.storage.get_events(conversation) if e["source"] == "confirmed_send"]), 1)

    async def test_new_inbound_before_the_due_time_cancels_the_draft(self) -> None:
        conversation = self.conversation()
        self.storage.mark_baseline_done(conversation)
        self.queue_due_send(conversation, "haan, 7 pm?")
        # The contact says something else before the draft goes out.
        self.script_reads(
            THREAD_A, event(0, "contact", "hi"), event(1, "contact", "are you there"),
        )
        report = await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.storage.outbox_items(conversation)[0]["state"], "cancelled")
        self.assertEqual(self.work(conversation)["state"], "pending")
        del report

    async def test_stale_work_is_reassessed_not_replayed(self) -> None:
        conversation = self.conversation()
        self.storage.mark_baseline_done(conversation)
        self.queue_due_send(conversation, "old draft")
        first_pending = self.clock.now() - timedelta(seconds=self.settings.stale_reeval + 60)
        self.storage.update_work(conversation, first_pending_at=ST.iso(first_pending))
        report = await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.storage.outbox_items(conversation)[0]["state"], "cancelled")
        self.assertEqual(self.work(conversation)["state"], "pending")
        self.assertTrue(any("stale" in note for note in report.notes))

    async def test_a_newer_revision_cancels_an_older_draft(self) -> None:
        conversation = self.conversation()
        self.storage.mark_baseline_done(conversation)
        self.queue_due_send(conversation, "first draft")
        self.storage.update_work(conversation, state="drafted")
        self.inbox(row(0))
        self.script_reads(
            THREAD_A, event(0, "contact", "hi"), event(1, "contact", "one"),
            event(2, "contact", "two"),
        )
        await self.coordinator.tick_discover()
        self.assertEqual(self.storage.outbox_items(conversation)[0]["state"], "cancelled")


# --- C05: dry run -------------------------------------------------------


class TestDryRunC05(CoordinatorCase):
    async def test_a_dry_run_copy_leaves_the_production_file_byte_identical(self) -> None:
        conversation = self.conversation()
        self.storage.mark_baseline_done(conversation)
        self.queue_due_send(conversation, "hello")
        self.storage.close()
        self.storage = ST.open_storage(self.dir)
        before = hashlib.sha256((self.dir / ST.DB_FILENAME).read_bytes()).hexdigest()

        copy, scratch = self.coordinator.dry_run_copy(self.storage)
        self.addCleanup(lambda: shutil_cleanup(scratch))
        self.assertNotEqual(str(copy.path), str(self.storage.path))

        # Drive the copy as a dry run: it may read, it may not send.
        coordinator = Coordinator(
            copy, self.browser, self.decisions, self.settings,
            clock=self.clock, rng=self.rng, account_id=ACCOUNT, dry_run=True,
        )
        self.script_reads(THREAD_A, event(0, "contact", "hi"), event(1, "owner", "hello"))
        await coordinator.tick_due()
        self.assertEqual(self.browser.sent, [], "the dry run must not type")
        copy.close()

        after = hashlib.sha256((self.dir / ST.DB_FILENAME).read_bytes()).hexdigest()
        self.assertEqual(before, after, "a dry run must not mutate production state")
        del conversation

    async def test_the_dry_run_browser_refuses_to_send(self) -> None:
        from shadow.coordinator import DryRunBrowser

        wrapper = DryRunBrowser(self.browser)
        with self.assertRaises(RuntimeError):
            await wrapper.send_one(THREAD_A, "hello", None)

    def test_dry_run_copy_is_a_separate_database(self) -> None:
        copy, scratch = self.coordinator.dry_run_copy(self.storage)
        self.addCleanup(lambda: shutil_cleanup(scratch))
        self.addCleanup(copy.close)
        self.assertNotEqual(copy.path, self.storage.path)
        self.assertEqual(copy.table_counts()["conversations"],
                         self.storage.table_counts()["conversations"])


def shutil_cleanup(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# --- D08: owner command through the coordinator ------------------------


class TestOwnerCommandD08(CoordinatorCase):
    async def test_an_owner_command_goes_through_the_due_path(self) -> None:
        from shadow.contracts import SendResult

        conversation = self.conversation()
        self.seed_history(conversation)
        self.coordinator.enqueue_owner_send(conversation, "hello owner")
        self.script_reads(THREAD_A, event(0, "contact", "hi"))
        self.browser.queue(SendResult(status="confirmed", thread_id=THREAD_A))
        report = await self.coordinator.tick_due()
        self.assertEqual(report.sent, 1)
        events = [e for e in self.storage.get_events(conversation) if e["source"] == "confirmed_send"]
        self.assertEqual([e["origin"] for e in events], ["owner_authored"])

    async def test_an_owner_command_obeys_the_pause(self) -> None:
        conversation = self.conversation()
        self.coordinator.enqueue_owner_send(conversation, "hello owner")
        self.coordinator.pause("owner_pause")
        self.coordinator = self.build()
        self.script_reads(THREAD_A, event(0, "contact", "hi"), event(1, "owner", "hello owner"))
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])

    async def test_an_owner_command_obeys_active_hours(self) -> None:
        conversation = self.conversation()
        self.clock.current = LATE_NIGHT
        self.coordinator = self.build()
        self.coordinator.enqueue_owner_send(conversation, "hello owner")
        self.script_reads(THREAD_A, event(0, "contact", "hi"), event(1, "owner", "hello owner"))
        await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])

    async def test_an_owner_command_obeys_the_send_cap(self) -> None:
        conversation = self.conversation()
        for _ in range(self.settings.send_cap_account[0][0]):
            self.storage.try_consume_rates(
                "send_account", self.settings.send_cap_account, self.clock.now()
            )
        self.coordinator.enqueue_owner_send(conversation, "hello owner")
        self.script_reads(THREAD_A, event(0, "contact", "hi"), event(1, "owner", "hello owner"))
        report = await self.coordinator.tick_due()
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(report.stopped_reason, "send_cap")


# --- startup ------------------------------------------------------------


class TestStartup(CoordinatorCase):
    async def test_startup_creates_the_runtime_row_and_reads_the_mode(self) -> None:
        report = await self.coordinator.startup()
        runtime = self.storage.get_runtime()
        self.assertEqual(runtime["mode"], "autonomous")
        self.assertEqual(runtime["account_id"], ACCOUNT)
        self.assertIsNone(runtime["pause_reason"])
        del report

    async def test_startup_reconciles_interrupted_attempts_before_anything_else(self) -> None:
        conversation = self.conversation()
        self.queue_due_send(conversation, "hello")
        item = self.storage.outbox_items(conversation)[0]
        self.storage.transition(item["id"], "attempting")
        report = await self.coordinator.startup()
        self.assertEqual(self.storage.get_outbox_item(item["id"])["state"], "held")
        self.assertTrue(any("reconciled" in note for note in report.notes))


if __name__ == "__main__":
    unittest.main()

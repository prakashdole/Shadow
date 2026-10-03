"""P03 storage migration v2 (S01-S06, C03, C11 groundwork).

What v2 adds and what these tests pin:

* a populated v1 database migrates to v2 with a VERIFIED pre-migration backup
  that still contains the v1 shape, and no data is lost or silently changed;
* the widened CHECK constraints are enforced (a v1-only status is refused);
* `try_consume` / `try_consume_rates` hold at the cap boundary and consume
  nothing when exhausted, and the window rolls over;
* `raise_alert` is idempotent on its dedupe key.

Every database here is a fresh temporary directory. The owner's real database is
never opened.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shadow import storage as ST

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)


def _lunch(hour: int = 12) -> datetime:
    return NOW.replace(hour=hour)


class V2Case(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="shadow-v2-"))
        self.addCleanup(self._rmtree)
        self.storage = ST.open_storage(self.dir)
        self.addCleanup(self._close)

    def _close(self) -> None:
        if self.storage is not None and self.storage.conn is not None:
            self.storage.close()

    def _rmtree(self) -> None:
        import shutil

        for suffix in ("-wal", "-shm"):
            side = self.storage.path.with_name(self.storage.path.name + suffix)
            if side.exists():
                side.unlink()
        shutil.rmtree(self.dir, ignore_errors=True)

    def conversation(self, thread_id: str = "34028236684171030094912811223344556", label="Test One"):
        return self.storage.get_or_create_conversation("acct-test", thread_id, label)


class TestFreshV2(V2Case):
    def test_current_version_is_two_and_v1_sql_is_untouched(self) -> None:
        self.assertEqual(ST.CURRENT_VERSION, 7)
        self.assertEqual(
            self.storage.conn.execute("PRAGMA user_version").fetchone()[0], ST.CURRENT_VERSION
        )
        # v1 is still described by v1 SQL: a fresh database applies both, and
        # the v1 statements are byte-identical to what P01 shipped.
        self.assertEqual(len(ST.MIGRATIONS), 7)
        self.assertEqual(ST.MIGRATIONS[0][0], 1)
        self.assertIn("CREATE TABLE outbox", ST.MIGRATIONS[0][1])

    def test_new_tables_and_columns_exist(self) -> None:
        counts = self.storage.table_counts()
        for table in ("usage_counters", "alerts"):
            self.assertIn(table, counts)
        columns = {
            row["name"]
            for row in self.storage.conn.execute("PRAGMA table_info(conversations)")
        }
        self.assertIn("paused_reason", columns)
        self.assertIn("first_seen_baseline_done", columns)
        work = {row["name"] for row in self.storage.conn.execute("PRAGMA table_info(conversation_work)")}
        self.assertLessEqual({"sampled_delay_seconds", "last_inbound_at", "held_reason"}, work)
        outbox = {row["name"] for row in self.storage.conn.execute("PRAGMA table_info(outbox)")}
        self.assertIn("attempts", outbox)

    def test_widened_work_and_decision_states_are_accepted(self) -> None:
        conversation = self.conversation()
        for state in (
            "idle", "pending", "assessing", "drafted", "awaiting_approval", "due",
            "sending", "held", "owner_active", "blocked",
        ):
            self.storage.set_work(
                conversation, inbound_revision=1, state=state, cancel_generation=0
            )
            self.assertEqual(self.storage.get_work(conversation)["state"], state)
        for status in (
            "recorded", "awaiting_approval", "approved", "rejected", "expired",
            "superseded", "queued", "done",
        ):
            decision = self.storage.add_decision(
                conversation_id=conversation,
                source_revision=1,
                record_json="{}",
                versions_json="{}",
                status=status,
            )
            self.assertEqual(self.storage.decisions_for(conversation)[-1]["status"], status)
            self.storage.set_decision_status(decision, "recorded")

    def test_v1_only_status_is_refused(self) -> None:
        conversation = self.conversation()
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.conn.execute(
                "INSERT INTO decisions (conversation_id, source_revision, record_json, "
                "versions_json, status, created_at) VALUES (?, 1, '{}', '{}', 'pending', 'x')",
                (conversation,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.conn.execute(
                "INSERT INTO decisions (conversation_id, source_revision, record_json, "
                "versions_json, status, created_at) VALUES (?, 1, '{}', '{}', 'completed', 'x')",
                (conversation,),
            )

    def test_rebuilt_decisions_still_has_a_live_outbox_foreign_key(self) -> None:
        conversation = self.conversation()
        decision = self.storage.add_decision(
            conversation_id=conversation, source_revision=1, record_json="{}", versions_json="{}"
        )
        self.storage.add_outbox_item(
            decision_id=decision, item_index=0, conversation_id=conversation,
            text="hello", authority="autonomous", revision=1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.conn.execute(
                "INSERT INTO outbox (decision_id, item_index, conversation_id, text, "
                "authority, revision, state) VALUES (999, 0, ?, 'x', 'autonomous', 1, 'pending')",
                (conversation,),
            )
        self.assertEqual(self.storage.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.storage.conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class TestMigrationFromPopulatedV1(V2Case):
    """A database written by P01's code must arrive at v2 with its data."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="shadow-v1-"))
        self.addCleanup(self._rmtree_v1)
        self.storage = None
        self._build_v1_database()
        self.storage = ST.open_storage(self.dir)
        self.addCleanup(self._close)

    def _rmtree_v1(self) -> None:
        import shutil

        if self.storage is not None and self.storage.conn is not None:
            for suffix in ("-wal", "-shm"):
                side = self.storage.path.with_name(self.storage.path.name + suffix)
                if side.exists():
                    side.unlink()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _build_v1_database(self) -> None:
        path = self.dir / ST.DB_FILENAME
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            for statement in ST._statements(ST.SCHEMA_V1):
                conn.execute(statement)
            conn.execute("PRAGMA user_version = 1")
            conn.execute(
                "INSERT INTO conversations (id, account_id, thread_id, label) "
                "VALUES (1, 'acct-test', '34028236684171030094912811223344556', 'Test One')"
            )
            conn.execute(
                "INSERT INTO events (conversation_id, sender, kind, text, observed_at, "
                "source, origin, seq) VALUES (1, 'contact', 'text', 'kal milte hai?', "
                "'2026-09-01T00:00:00.000000Z', 'dom', 'unknown', 1)"
            )
            conn.execute(
                "INSERT INTO conversation_work (conversation_id, inbound_revision, due_at, "
                "first_pending_at, state, cancel_generation) VALUES "
                "(1, 3, '2026-09-02T00:00:00.000000Z', '2026-09-01T00:00:00.000000Z', "
                "'blocked', 7)"
            )
            conn.execute(
                "INSERT INTO decisions (conversation_id, source_revision, record_json, "
                "versions_json, status, created_at) VALUES (1, 3, '{\"a\":1}', '{\"v\":2}', "
                "'pending', '2026-09-01T00:00:00.000000Z')"
            )
            conn.execute(
                "INSERT INTO decisions (conversation_id, source_revision, record_json, "
                "versions_json, status, created_at) VALUES (1, 3, '{\"b\":2}', '{\"v\":2}', "
                "'completed', '2026-09-01T00:00:00.000000Z')"
            )
            conn.execute(
                "INSERT INTO outbox (decision_id, item_index, conversation_id, text, "
                "authority, revision, state) VALUES (1, 0, 1, 'reply one', 'autonomous', 3, "
                "'pending')"
            )
            conn.execute(
                "INSERT INTO owner_latency_profile (bucket, samples, p25, p50, p75, "
                "computed_at) VALUES ('06-12', 12, 20.0, 40.0, 90.0, '2026-09-01T00:00:00Z')"
            )
        finally:
            conn.close()

    def test_migrated_to_v2_with_data_intact(self) -> None:
        self.assertEqual(
            self.storage.conn.execute("PRAGMA user_version").fetchone()[0], ST.CURRENT_VERSION
        )
        conversation = self.storage.get_conversation(1)
        self.assertEqual(conversation["thread_id"], "34028236684171030094912811223344556")
        self.assertEqual(conversation["contact_status"], "unknown_pending")
        self.assertEqual(conversation["first_seen_baseline_done"], 0)
        self.assertIsNone(conversation["paused_reason"])
        self.assertEqual([e["text"] for e in self.storage.get_events(1)], ["kal milte hai?"])

    def test_v1_work_row_survives_with_its_blocked_state_mapped(self) -> None:
        work = self.storage.get_work(1)
        self.assertEqual(work["inbound_revision"], 3)
        self.assertEqual(work["cancel_generation"], 7)
        self.assertEqual(work["first_pending_at"], "2026-09-01T00:00:00.000000Z")
        # v1 'blocked' is a hold; v2 spells it 'held' so the coordinator has one
        # name for a thread that is not workable. Nothing is dropped.
        self.assertEqual(work["state"], "held")
        self.assertIsNone(work["sampled_delay_seconds"])

    def test_v1_decision_statuses_are_mapped_forward(self) -> None:
        statuses = [d["status"] for d in self.storage.decisions_for(1)]
        self.assertEqual(statuses, ["recorded", "done"])
        self.assertEqual(self.storage.decisions_for(1)[0]["record_json"], '{"a":1}')

    def test_outbox_item_survives_and_can_still_be_driven(self) -> None:
        items = self.storage.outbox_items(1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "pending")
        self.assertEqual(items[0]["text"], "reply one")
        self.storage.transition(items[0]["id"], "attempting")
        self.assertEqual(self.storage.get_outbox_item(items[0]["id"])["attempts"], 1)

    def test_latency_profile_survives(self) -> None:
        self.assertEqual(self.storage.latency_profile()["06-12"]["p50"], 40.0)

    def test_a_verified_v1_backup_exists_and_holds_the_v1_shape(self) -> None:
        backups = sorted((self.dir / ST.BACKUP_DIR).glob("pre-migration-v1-*.sqlite3"))
        self.assertEqual(len(backups), 1, "one backup per migration step, labelled with the "
                                         "version migrated FROM")
        report = self.storage.restore_check(backups[0], self.dir / "verify-v1")
        self.assertEqual(report["quick_check"], "ok")
        counts = report["row_counts"]
        # The pre-migration copy is genuinely v1: v2's tables are absent from it.
        self.assertNotIn("alerts", counts)
        self.assertNotIn("usage_counters", counts)
        self.assertEqual(counts["events"], 1)
        self.assertEqual(counts["outbox"], 1)
        with closing_readonly(backups[0]) as copy:
            self.assertEqual(
                copy.execute("PRAGMA user_version").fetchone()[0], 1
            )
            self.assertEqual(
                copy.execute("SELECT status FROM decisions ORDER BY id").fetchall()[0][0],
                "pending",
            )


def closing_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


class TestUsageCounters(V2Case):
    def test_try_consume_holds_at_the_boundary_and_writes_nothing_when_full(self) -> None:
        first = self.storage.try_consume("navigation", 2, 3600, NOW)
        second = self.storage.try_consume("navigation", 2, 3600, NOW)
        third = self.storage.try_consume("navigation", 2, 3600, NOW)
        self.assertEqual([first, second, third], [True, True, False])
        self.assertEqual(self.storage.usage_count("navigation", 3600, NOW), 2)
        self.assertEqual(
            len([r for r in self.storage.usage_counters() if r["kind"] == "navigation"]), 1
        )

    def test_the_window_rolls_over(self) -> None:
        self.assertTrue(self.storage.try_consume("navigation", 1, 3600, NOW))
        self.assertFalse(self.storage.try_consume("navigation", 1, 3600, NOW + timedelta(minutes=30)))
        later = NOW + timedelta(hours=1, seconds=1)
        self.assertTrue(self.storage.try_consume("navigation", 1, 3600, later))
        self.assertEqual(self.storage.usage_count("navigation", 3600, NOW), 1)
        self.assertEqual(self.storage.usage_count("navigation", 3600, later), 1)

    def test_kinds_are_counted_separately(self) -> None:
        self.storage.try_consume("send_account", 1, 3600, NOW)
        self.assertFalse(self.storage.try_consume("send_account", 1, 3600, NOW))
        self.assertTrue(self.storage.try_consume("send_thread:1", 1, 3600, NOW))
        self.assertTrue(self.storage.try_consume("send_thread:2", 1, 3600, NOW))
        self.assertEqual(self.storage.usage_count("send_thread:1", 3600, NOW), 1)
        self.assertEqual(self.storage.usage_count("send_thread:2", 3600, NOW), 1)

    def test_multi_window_rates_refuse_without_consuming_anything(self) -> None:
        rates = ((2, 3600), (1, 86400))
        self.assertTrue(self.storage.try_consume_rates("send_account", rates, NOW))
        # The hourly rate has room, the daily one is used up: the whole call is
        # refused and the hourly counter must not have been burned.
        self.assertFalse(self.storage.try_consume_rates("send_account", rates, NOW))
        self.assertEqual(self.storage.usage_count("send_account", 3600, NOW), 1)
        self.assertEqual(self.storage.usage_count("send_account", 86400, NOW), 1)

    def test_a_zero_cap_refuses_every_use(self) -> None:
        self.assertFalse(self.storage.try_consume_rates("send_account", ((0, 3600),), NOW))
        self.assertEqual(self.storage.usage_counters(), [])

    def test_a_zero_or_negative_window_is_refused(self) -> None:
        with self.assertRaises(ST.StorageError):
            self.storage.try_consume("navigation", 1, 0, NOW)

    def test_a_naive_clock_is_refused_rather_than_assumed_utc(self) -> None:
        with self.assertRaises(ST.StorageError):
            self.storage.try_consume("navigation", 1, 3600, datetime(2026, 9, 28, 10, 0))

    def test_default_now_is_used_when_no_clock_is_passed(self) -> None:
        self.assertTrue(self.storage.try_consume("navigation", 1, 3600))
        self.assertEqual(len(self.storage.usage_counters()), 1)


class TestAlerts(V2Case):
    def test_raise_alert_is_idempotent_on_its_dedupe_key(self) -> None:
        conversation = self.conversation()
        first = self.storage.raise_alert(
            dedupe_key="unknown_contact:1", tier="action", reason="unknown_contact",
            conversation_id=conversation,
        )
        second = self.storage.raise_alert(
            dedupe_key="unknown_contact:1", tier="action", reason="unknown_contact",
            conversation_id=conversation,
        )
        self.assertEqual(first, second)
        alerts = self.storage.alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["state"], "pending")
        self.assertEqual(alerts[0]["tier"], "action")
        self.assertEqual(alerts[0]["attempts"], 0)
        self.assertEqual(alerts[0]["digest_included"], 0)
        self.assertEqual(alerts[0]["conversation_id"], conversation)

    def test_a_resolved_alert_is_not_re_raised(self) -> None:
        alert = self.storage.raise_alert(dedupe_key="k", tier="info", reason="preview_only")
        self.storage.set_alert_state(alert, "resolved")
        again = self.storage.raise_alert(dedupe_key="k", tier="info", reason="preview_only")
        self.assertEqual(again, alert)
        self.assertEqual(self.storage.alerts(state="resolved")[0]["state"], "resolved")

    def test_tier_and_state_are_constrained(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.conn.execute(
                "INSERT INTO alerts (dedupe_key, tier, reason, state, created_at) "
                "VALUES ('k', 'catastrophic', 'x', 'pending', 'y')"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.conn.execute(
                "INSERT INTO alerts (dedupe_key, tier, reason, state, created_at) "
                "VALUES ('k', 'info', 'x', 'shrugged', 'y')"
            )

    def test_alerts_filter_by_state_and_conversation(self) -> None:
        conversation = self.conversation()
        one = self.storage.raise_alert(dedupe_key="a", tier="info", reason="r", conversation_id=conversation)
        self.storage.raise_alert(dedupe_key="b", tier="urgent", reason="r", conversation_id=conversation)
        self.storage.raise_alert(dedupe_key="c", tier="urgent", reason="r")
        self.storage.set_alert_state(one, "sent", attempts=2, next_attempt_at="2026-09-28T11:00:00.000000Z")
        self.assertEqual([a["dedupe_key"] for a in self.storage.alerts(state="pending")], ["b", "c"])
        self.assertEqual(
            [a["dedupe_key"] for a in self.storage.alerts(conversation_id=conversation)], ["a", "b"]
        )
        sent = self.storage.alerts(state="sent")[0]
        self.assertEqual(sent["attempts"], 2)
        self.assertEqual(sent["next_attempt_at"], "2026-09-28T11:00:00.000000Z")


class TestWorkAndCancellation(V2Case):
    def test_update_work_changes_only_named_columns(self) -> None:
        conversation = self.conversation()
        self.storage.set_work(
            conversation, inbound_revision=2, due_at="2026-09-28T12:00:00.000000Z",
            first_pending_at="2026-09-28T11:00:00.000000Z", state="pending", cancel_generation=1,
        )
        self.storage.update_work(
            conversation, state="held", held_reason="manual_text",
            last_inbound_at="2026-09-28T11:30:00.000000Z", sampled_delay_seconds=42,
        )
        work = self.storage.get_work(conversation)
        self.assertEqual(work["state"], "held")
        self.assertEqual(work["held_reason"], "manual_text")
        self.assertEqual(work["sampled_delay_seconds"], 42)
        # untouched
        self.assertEqual(work["inbound_revision"], 2)
        self.assertEqual(work["cancel_generation"], 1)
        self.assertEqual(work["due_at"], "2026-09-28T12:00:00.000000Z")

    def test_update_work_refuses_an_unknown_column_and_a_missing_row(self) -> None:
        conversation = self.conversation()
        with self.assertRaises(ST.StorageError):
            self.storage.update_work(conversation, nonsense=1)
        with self.assertRaises(ST.StorageError):
            self.storage.update_work(conversation, state="pending")
        self.storage.set_work(conversation, inbound_revision=0)
        self.assertIsNone(self.storage.update_work(conversation))

    def test_work_in_states_joins_the_conversation(self) -> None:
        first = self.conversation("100200300")
        second = self.conversation("100200301", "Test Two")
        self.storage.set_work(first, inbound_revision=1, state="due")
        self.storage.set_work(second, inbound_revision=1, state="held")
        rows = {w["conversation_id"]: w for w in self.storage.work_in_states(("due", "held"))}
        self.assertEqual(sorted(rows), [first, second])
        self.assertEqual(rows[first]["thread_id"], "100200300")
        self.assertEqual(rows[first]["contact_status"], "unknown_pending")

    def test_cancel_outbox_items_leaves_attempted_items_alone(self) -> None:
        conversation = self.conversation()
        pending = self._item(conversation, 0)
        attempted = self._item(conversation, 1)
        confirmed = self._item(conversation, 2)
        self.storage.transition(attempted, "attempting")
        self.storage.transition(confirmed, "attempting")
        self.storage.confirm_send(confirmed, {"text": "sent", "seq": 99})
        cancelled = self.storage.cancel_outbox_items(conversation, "takeover")
        self.assertEqual(cancelled, 1)
        self.assertEqual(self.storage.get_outbox_item(pending)["state"], "cancelled")
        self.assertEqual(self.storage.get_outbox_item(attempted)["state"], "attempting")
        self.assertEqual(self.storage.get_outbox_item(confirmed)["state"], "confirmed")

    def test_cancel_decisions_only_touches_the_named_statuses(self) -> None:
        conversation = self.conversation()
        recorded = self.storage.add_decision(
            conversation_id=conversation, source_revision=1, record_json="{}", versions_json="{}"
        )
        done = self.storage.add_decision(
            conversation_id=conversation, source_revision=1, record_json="{}", versions_json="{}",
            status="done",
        )
        self.storage.cancel_decisions(conversation, ("recorded", "awaiting_approval", "approved"))
        statuses = {d["id"]: d["status"] for d in self.storage.decisions_for(conversation)}
        self.assertEqual(statuses[recorded], "superseded")
        self.assertEqual(statuses[done], "done")

    def test_conversation_helpers(self) -> None:
        conversation = self.conversation()
        self.assertEqual(
            self.storage.get_conversation_by_thread("acct-test", "34028236684171030094912811223344556")["id"],
            conversation,
        )
        self.assertIsNone(self.storage.get_conversation_by_thread("acct-test", "999"))
        self.storage.set_owner_active_until(conversation, "2026-09-28T11:00:00.000000Z")
        self.assertEqual(
            self.storage.get_conversation(conversation)["owner_active_until"],
            "2026-09-28T11:00:00.000000Z",
        )
        self.storage.set_owner_active_until(conversation, None)
        self.assertIsNone(self.storage.get_conversation(conversation)["owner_active_until"])
        self.storage.set_paused_reason(conversation, "manual_text")
        self.assertEqual(self.storage.get_conversation(conversation)["paused_reason"], "manual_text")
        self.storage.mark_baseline_done(conversation)
        self.assertEqual(self.storage.get_conversation(conversation)["first_seen_baseline_done"], 1)

    def test_baseline_flag_is_constrained_to_zero_or_one(self) -> None:
        conversation = self.conversation()
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.conn.execute(
                "UPDATE conversations SET first_seen_baseline_done = 2 WHERE id = ?",
                (conversation,),
            )

    def _item(self, conversation: int, index: int) -> int:
        decision = self.storage.add_decision(
            conversation_id=conversation, source_revision=1, record_json="{}", versions_json="{}"
        )
        return self.storage.add_outbox_item(
            decision_id=decision, item_index=index, conversation_id=conversation,
            text=f"item {index}", authority="autonomous", revision=1,
        )


class TestTimestampHelpers(unittest.TestCase):
    def test_iso_requires_an_aware_datetime(self) -> None:
        with self.assertRaises(ST.StorageError):
            ST.iso(datetime(2026, 9, 28, 10, 0))
        self.assertTrue(ST.iso(NOW).endswith("Z"))

    def test_parse_iso_round_trips_and_treats_null_as_absent(self) -> None:
        self.assertIsNone(ST.parse_iso(None))
        self.assertIsNone(ST.parse_iso(""))
        self.assertEqual(ST.parse_iso(ST.iso(NOW)), NOW)

    def test_iso_converts_to_utc(self) -> None:
        plus_two = timezone(timedelta(hours=2))
        self.assertEqual(ST.iso(NOW.astimezone(plus_two)), ST.iso(NOW))


if __name__ == "__main__":
    unittest.main()

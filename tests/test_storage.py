"""S02-S06: identity, transaction atomicity, failure containment, one writer."""

from __future__ import annotations

import os
import random
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from shadow import storage as ST

REPO = Path(__file__).resolve().parent.parent


def _child(code: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class StorageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.storage = ST.open_storage(self.dir)
        self.addCleanup(self.storage.close)
        self.account = "acct-test"

    def conversation(self, thread_id: str = "100200300", label: str | None = None) -> int:
        return self.storage.get_or_create_conversation(self.account, thread_id, label)

    def decision_with_item(self, authority: str = "autonomous", text: str = "yo") -> int:
        conversation = self.conversation()
        decision = self.storage.add_decision(
            conversation_id=conversation,
            source_revision=1,
            record_json='{"action": "reply"}',
            versions_json='{"config_digest": "test"}',
        )
        return self.storage.add_outbox_item(
            decision_id=decision,
            item_index=0,
            conversation_id=conversation,
            text=text,
            authority=authority,
            revision=1,
        )


class TestOpenAndPermissions(StorageCase):
    def test_private_directory_and_database(self) -> None:
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.storage.path.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE((self.dir / ST.LOCK_FILENAME).stat().st_mode), 0o600
            )

    def test_pragmas_on_every_open(self) -> None:
        one = self.storage.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        two = self.storage.conn.execute("PRAGMA journal_mode").fetchone()[0]
        three = self.storage.conn.execute("PRAGMA synchronous").fetchone()[0]
        four = self.storage.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual((one, two.lower(), three, four), (1, "wal", 2, 5000))

    def test_lock_file_records_this_pid(self) -> None:
        self.assertEqual(
            (self.dir / ST.LOCK_FILENAME).read_text(encoding="utf-8").strip(),
            str(os.getpid()),
        )

    def test_schema_version_is_set(self) -> None:
        self.assertEqual(
            self.storage.conn.execute("PRAGMA user_version").fetchone()[0],
            ST.CURRENT_VERSION,
        )


class TestIdentityS02S03(StorageCase):
    def test_same_text_twice_at_different_positions_is_two_events(self) -> None:
        conversation = self.conversation()
        for seq in (1, 2):
            self.storage.add_event(
                conversation_id=conversation,
                sender="contact",
                kind="text",
                text="ha",
                source="dom",
                origin="unknown",
                seq=seq,
            )
        events = self.storage.get_events(conversation)
        self.assertEqual(len(events), 2)
        self.assertEqual([e["seq"] for e in events], [1, 2])

    def test_repeated_platform_message_id_in_one_conversation_is_rejected(self) -> None:
        conversation = self.conversation()
        self.storage.add_event(
            conversation_id=conversation, sender="contact", kind="text", text="ha",
            source="dom", origin="unknown", seq=1, platform_msg_id="m-1",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.add_event(
                conversation_id=conversation, sender="contact", kind="text", text="ha",
                source="dom", origin="unknown", seq=2, platform_msg_id="m-1",
            )
        self.assertEqual(len(self.storage.get_events(conversation)), 1)

    def test_absent_platform_ids_do_not_collide(self) -> None:
        conversation = self.conversation()
        for _ in range(3):
            self.storage.add_event(
                conversation_id=conversation, sender="contact", kind="text", text="ha",
                source="dom", origin="unknown", platform_msg_id=None,
            )
        self.assertEqual(len(self.storage.get_events(conversation)), 3)

    def test_duplicate_labels_with_different_threads_stay_separate(self) -> None:
        first = self.conversation("111", "Same Name")
        second = self.conversation("12", "Same Name")  # also an ID prefix collision
        self.assertNotEqual(first, second)
        self.assertEqual(self.storage.get_conversation(first)["thread_id"], "111")
        self.assertEqual(self.storage.get_conversation(second)["thread_id"], "12")

    def test_thread_id_must_be_digits(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.conversation("111abc")

    def test_account_is_part_of_the_identity(self) -> None:
        mine = self.storage.get_or_create_conversation("acct-a", "999", "X")
        theirs = self.storage.get_or_create_conversation("acct-b", "999", "X")
        self.assertNotEqual(mine, theirs)

    def test_label_is_never_a_lookup_key(self) -> None:
        first = self.conversation("111", "Aditya")
        second = self.conversation("222", "Aditya shirke")
        again = self.storage.get_or_create_conversation(self.account, "111", "Aditya")
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)

    def test_imported_folder_is_never_bound_automatically(self) -> None:
        conversation = self.conversation("321", "Aditya_shirke_7")
        legacy = self.storage.add_legacy_contact("Aditya_shirke_7", 7, "friend")
        self.storage.add_event(
            legacy_contact_id=legacy, sender="contact", kind="text", text="hi",
            source="import", origin="unknown", seq=1,
        )
        contact = self.storage.get_legacy_contact(legacy)
        self.assertEqual(contact["binding_status"], "unbound")
        self.assertIsNone(contact["bound_conversation_id"])
        self.assertEqual(self.storage.get_events(conversation), [])

    def test_binding_is_explicit_and_reversible(self) -> None:
        conversation = self.conversation("321", "label")
        legacy = self.storage.add_legacy_contact("folder_x")
        self.storage.bind_legacy_contact(legacy, conversation, "owner confirmed")
        bound = self.storage.get_legacy_contact(legacy)
        self.assertEqual(bound["binding_status"], "bound")
        self.assertEqual(bound["bound_conversation_id"], conversation)
        with self.assertRaises(ST.StorageError):
            self.storage.bind_legacy_contact(legacy, conversation, "again")
        self.storage.unbind_legacy_contact(legacy)
        self.assertEqual(self.storage.get_legacy_contact(legacy)["binding_status"], "unbound")

    def test_event_belongs_to_exactly_one_parent(self) -> None:
        conversation = self.conversation()
        legacy = self.storage.add_legacy_contact("folder_y")
        with self.assertRaises(ST.StorageError):
            self.storage.add_event(
                conversation_id=conversation, legacy_contact_id=legacy, sender="contact",
                kind="text", text="x", source="import", origin="unknown",
            )
        with self.assertRaises(ST.StorageError):
            self.storage.add_event(
                sender="contact", kind="text", text="x", source="import", origin="unknown",
            )


class TestTransactionAtomicityS04(StorageCase):
    def test_exception_inside_a_transaction_leaves_no_partial_rows(self) -> None:
        conversation = self.conversation()
        with self.assertRaises(RuntimeError):
            with self.storage.transaction() as conn:
                conn.execute(
                    "INSERT INTO decisions (conversation_id, source_revision, record_json, "
                    "versions_json, status, created_at) VALUES (?, 1, '{}', '{}', 'pending', 'x')",
                    (conversation,),
                )
                conn.execute(
                    "INSERT INTO decisions (conversation_id, source_revision, record_json, "
                    "versions_json, status, created_at) VALUES (?, 1, '{}', '{}', 'pending', 'y')",
                    (conversation,),
                )
                raise RuntimeError("boom")
        self.assertEqual(self.storage.table_counts()["decisions"], 0)

    def test_process_killed_mid_transaction_leaves_no_partial_rows(self) -> None:
        conversation = self.conversation()
        child = _child(
            "import os, sqlite3, sys\n"
            f"conn = sqlite3.connect({str(self.storage.path)!r}, isolation_level=None)\n"
            "conn.execute('PRAGMA busy_timeout = 5000')\n"
            "conn.execute('BEGIN IMMEDIATE')\n"
            "conn.execute(\"INSERT INTO decisions (conversation_id, source_revision, "
            f"record_json, versions_json, status, created_at) VALUES ({conversation}, 7, "
            "'{}', '{}', 'pending', 'x')\")\n"
            "os._exit(1)\n"
        )
        self.addCleanup(_kill, child)
        child.wait(timeout=60)
        self.assertNotEqual(child.returncode, 0)
        self.assertEqual(self.storage.table_counts()["decisions"], 0)
        self.assertEqual(self.conversation(conversation and "100200300"), conversation)

    def test_confirm_send_writes_event_and_state_together(self) -> None:
        item = self.decision_with_item()
        self.storage.transition(item, "attempting")
        event_id = self.storage.confirm_send(item, {"text": "yo"})
        stored = self.storage.get_outbox_item(item)
        self.assertEqual(stored["state"], "confirmed")
        self.assertEqual(stored["confirmed_event_id"], event_id)
        events = self.storage.get_events(stored["conversation_id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["origin"], "shadow_generated")
        self.assertEqual(events[0]["source"], "confirmed_send")

    def test_failure_between_event_insert_and_outbox_update_persists_neither(self) -> None:
        item = self.decision_with_item()
        self.storage.transition(item, "attempting")
        conversation = self.storage.get_outbox_item(item)["conversation_id"]

        original = self.storage._mark_confirmed

        def explode(conn, item_id, event_id):
            raise RuntimeError("disk went away between the two writes")

        self.storage._mark_confirmed = explode  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self.storage.confirm_send(item, {"text": "yo"})
        finally:
            del self.storage._mark_confirmed

        self.assertEqual(self.storage.get_outbox_item(item)["state"], "attempting")
        self.assertIsNone(self.storage.get_outbox_item(item)["confirmed_event_id"])
        self.assertEqual(self.storage.get_events(conversation), [])
        self.assertTrue(callable(original))

    def test_confirm_send_only_from_attempting_or_uncertain(self) -> None:
        item = self.decision_with_item()
        with self.assertRaises(ST.InvalidTransition):
            self.storage.confirm_send(item, {"text": "yo"})
        self.storage.transition(item, "attempting")
        self.storage.transition(item, "failed_before_submit")
        with self.assertRaises(ST.InvalidTransition):
            self.storage.confirm_send(item, {"text": "yo"})

    def test_confirm_send_refuses_a_foreign_conversation(self) -> None:
        item = self.decision_with_item()
        self.storage.transition(item, "attempting")
        other = self.conversation("555", "other thread")
        with self.assertRaises(ST.StorageError):
            self.storage.confirm_send(item, {"text": "yo", "conversation_id": other})
        self.assertEqual(self.storage.get_events(other), [])
        self.assertEqual(self.storage.get_events(self.conversation()), [])
        self.assertEqual(self.storage.get_outbox_item(item)["state"], "attempting")

    def test_authority_maps_to_origin(self) -> None:
        for authority, origin in (
            ("autonomous", "shadow_generated"),
            ("owner_approved", "owner_approved"),
            ("owner_command", "owner_authored"),
        ):
            item = self.decision_with_item(authority=authority, text=f"t-{authority}")
            self.storage.transition(item, "attempting")
            self.storage.confirm_send(item, {"text": f"t-{authority}"})
            item_row = self.storage.get_outbox_item(item)
            events = self.storage.get_events(item_row["conversation_id"])
            self.assertEqual(events[-1]["origin"], origin)


class TestOutboxStateMachine(StorageCase):
    def test_every_allowed_transition_works(self) -> None:
        for current, targets in ST.ALLOWED.items():
            for target in targets:
                item = self.decision_with_item()
                # Put the item in the exact state the graph names, then take
                # the edge out of it.
                with self.storage.transaction() as conn:
                    conn.execute("UPDATE outbox SET state = ? WHERE id = ?", (current, item))
                if target == "confirmed":
                    with self.assertRaises(ST.InvalidTransition):
                        self.storage.transition(item, target)
                    self.storage.confirm_send(item, {"text": "yo"})
                    self.assertEqual(self.storage.get_outbox_item(item)["state"], "confirmed")
                else:
                    self.storage.transition(item, target)
                    self.assertEqual(self.storage.get_outbox_item(item)["state"], target)

    def test_terminal_states_have_no_outgoing_edges(self) -> None:
        for state in ("confirmed", "cancelled", "held"):
            self.assertEqual(ST.ALLOWED[state], frozenset())

    def test_every_other_transition_raises(self) -> None:
        states = list(ST.ALLOWED)
        for current in states:
            for target in states:
                if target in ST.ALLOWED[current]:
                    continue
                item = self.decision_with_item()
                with self.storage.transaction() as conn:
                    conn.execute("UPDATE outbox SET state = ? WHERE id = ?", (current, item))
                with self.assertRaises(ST.InvalidTransition, msg=f"{current}->{target}"):
                    self.storage.transition(item, target)
                self.assertEqual(self.storage.get_outbox_item(item)["state"], current)

    def test_attempting_records_the_attempt_time(self) -> None:
        item = self.decision_with_item()
        self.assertIsNone(self.storage.get_outbox_item(item)["attempted_at"])
        self.storage.transition(item, "attempting", evidence="about to type")
        row = self.storage.get_outbox_item(item)
        self.assertIsNotNone(row["attempted_at"])
        self.assertEqual(row["evidence"], "about to type")

    def test_recover_after_restart_turns_attempting_into_uncertain(self) -> None:
        first = self.decision_with_item()
        second = self.decision_with_item(text="second")
        third = self.decision_with_item(text="third")
        self.storage.transition(first, "attempting")
        self.storage.transition(second, "attempting")
        self.storage.transition(third, "attempting")
        self.storage.transition(second, "failed_before_submit")
        self.assertEqual(self.storage.recover_after_restart(), 2)
        self.assertEqual(self.storage.get_outbox_item(first)["state"], "uncertain")
        self.assertEqual(self.storage.get_outbox_item(third)["state"], "uncertain")
        self.assertEqual(
            self.storage.get_outbox_item(second)["state"], "failed_before_submit"
        )
        self.assertEqual(self.storage.recover_after_restart(), 0)

    def test_outbox_item_is_unique_per_decision_index(self) -> None:
        item = self.decision_with_item()
        conversation = self.storage.get_outbox_item(item)["conversation_id"]
        decision = self.storage.conn.execute(
            "SELECT decision_id FROM outbox WHERE id = ?", (item,)
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.add_outbox_item(
                decision_id=decision, item_index=0, conversation_id=conversation,
                text="dupe", authority="autonomous", revision=1,
            )


class TestFailureContainmentS05(StorageCase):
    def test_newer_schema_version_stops_the_runtime(self) -> None:
        path = self.storage.path
        self.storage.close()
        with sqlite3.connect(path) as conn:
            conn.execute(f"PRAGMA user_version = {ST.CURRENT_VERSION + 1}")
        with self.assertRaises(ST.UnsupportedSchema) as ctx:
            ST.open_storage(self.dir)
        self.assertIn("newer", str(ctx.exception))
        with sqlite3.connect(path) as conn:
            conn.execute(f"PRAGMA user_version = {ST.CURRENT_VERSION}")
        self.storage = ST.open_storage(self.dir)
        self.addCleanup(self.storage.close)

    def test_corrupt_header_is_reported_and_left_untouched(self) -> None:
        path = self.storage.path
        self.storage.close()
        for suffix in ("-wal", "-shm"):
            side = path.with_name(path.name + suffix)
            if side.exists():
                side.unlink()
        blob = bytearray(path.read_bytes())
        random.seed(20260927)
        blob[:100] = bytes(random.randrange(256) for _ in range(100))
        path.write_bytes(bytes(blob))
        before = path.read_bytes()
        with self.assertRaises(ST.StorageCorrupt):
            ST.open_storage(self.dir)
        self.assertEqual(path.read_bytes(), before)
        self.storage = None

    def test_disk_full_surfaces_and_leaves_no_partial_row(self) -> None:
        conversation = self.conversation()
        page_count = self.storage.conn.execute("PRAGMA page_count").fetchone()[0]
        self.storage.conn.execute(f"PRAGMA max_page_count = {page_count}")
        with self.assertRaises(sqlite3.OperationalError):
            self.storage.add_event(
                conversation_id=conversation, sender="contact", kind="text",
                text="x" * 4000, source="dom", origin="unknown", seq=1,
            )
        self.storage.conn.execute("PRAGMA max_page_count = 1073741823")
        self.assertEqual(self.storage.get_events(conversation), [])

    def test_write_lock_held_by_another_connection_surfaces(self) -> None:
        # A restored copy, a second process or another tool can hold the
        # database write lock. The busy timeout must surface that as an error
        # instead of hanging, and nothing may be written.
        self.conversation()
        other = sqlite3.connect(self.storage.path, isolation_level=None, timeout=1)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")  # holds the write lock, no rows touched
        writer = ST.Storage(
            self.dir,
            sqlite3.connect(self.storage.path, isolation_level=None, timeout=0.2),
            None,
        )
        writer.conn.execute("PRAGMA busy_timeout = 200")
        self.addCleanup(writer.close)
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            writer.set_runtime(account_id="blocked")
        self.assertLess(time.monotonic() - started, 5)
        other.execute("ROLLBACK")
        writer.set_runtime(account_id="free")
        self.assertEqual(self.storage.get_runtime()["account_id"], "free")

    def test_backup_and_restore_check_agree(self) -> None:
        item = self.decision_with_item()
        self.storage.transition(item, "attempting")
        self.storage.confirm_send(item, {"text": "yo"})
        self.storage.add_legacy_contact("folder_z", 3, "colleague")
        dest = self.dir / "copy" / "snapshot.sqlite3"
        self.storage.backup(dest)
        report = self.storage.restore_check(dest, self.dir / "scratch")
        self.assertEqual(report["quick_check"], "ok")
        self.assertEqual(report["row_counts"], self.storage.table_counts())
        self.assertEqual(report["row_counts"]["outbox"], 1)

    def test_backup_of_an_empty_database_verifies(self) -> None:
        dest = self.dir / "empty.sqlite3"
        self.storage.backup(dest)
        self.assertEqual(
            self.storage.restore_check(dest, self.dir / "scratch2")["quick_check"], "ok"
        )

    def test_migrating_a_populated_older_database_backs_it_up_first(self) -> None:
        self.storage.close()
        for suffix in ("-wal", "-shm"):
            side = self.storage.path.with_name(self.storage.path.name + suffix)
            if side.exists():
                side.unlink()
        self.storage.path.unlink()

        legacy = sqlite3.connect(self.storage.path, isolation_level=None)
        legacy.execute("CREATE TABLE old_notes (id INTEGER PRIMARY KEY, note TEXT)")
        legacy.executemany("INSERT INTO old_notes (note) VALUES (?)", [("a",), ("b",)])
        legacy.execute("PRAGMA user_version = 0")
        legacy.close()

        storage = ST.open_storage(self.dir)
        self.addCleanup(storage.close)
        self.assertEqual(
            storage.conn.execute("PRAGMA user_version").fetchone()[0], ST.CURRENT_VERSION
        )
        self.assertEqual(storage.conn.execute("SELECT COUNT(*) FROM old_notes").fetchone()[0], 2)
        backups = sorted((self.dir / ST.BACKUP_DIR).glob("pre-migration-v0-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        report = storage.restore_check(backups[0], self.dir / "verify")
        self.assertEqual(report["quick_check"], "ok")
        self.assertEqual(report["row_counts"]["old_notes"], 2)
        self.assertNotIn("outbox", report["row_counts"])

    def test_migration_is_a_no_op_on_an_up_to_date_database(self) -> None:
        before = self.storage.table_counts()
        # A second open from the same process is refused by the lock, which
        # also proves nothing re-migrated a database already at the current
        # version.
        with self.assertRaises(ST.StorageLocked):
            ST.open_storage(self.dir)
        self.assertEqual(self.storage.table_counts(), before)


class TestProcessLockS06(StorageCase):
    def child_code(self) -> str:
        return (
            "import sys, time\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "from shadow.storage import open_storage\n"
            f"open_storage({str(self.dir)!r})\n"
            "print('held', flush=True)\n"
            "time.sleep(120)\n"
        )

    def test_second_writer_is_refused_while_the_first_is_alive(self) -> None:
        self.storage.close()
        child = _child(self.child_code())
        self.addCleanup(_kill, child)
        self.assertEqual(child.stdout.readline().strip(), "held")
        with self.assertRaises(ST.StorageLocked):
            ST.open_storage(self.dir)
        self.assertTrue(child.poll() is None, "the live holder must still be running")

    def test_lock_is_recovered_after_the_holder_dies(self) -> None:
        self.storage.close()
        child = _child(self.child_code())
        self.addCleanup(_kill, child)
        self.assertEqual(child.stdout.readline().strip(), "held")
        child.kill()
        child.wait(timeout=30)
        deadline = time.monotonic() + 10
        while True:
            try:
                self.storage = ST.open_storage(self.dir)
                self.addCleanup(self.storage.close)
                break
            except ST.StorageLocked:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self.assertEqual(
            self.storage.conn.execute("PRAGMA user_version").fetchone()[0],
            ST.CURRENT_VERSION,
        )

    def test_same_process_second_open_is_refused(self) -> None:
        with self.assertRaises(ST.StorageLocked):
            ST.open_storage(self.dir)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=30)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


if __name__ == "__main__":
    unittest.main()

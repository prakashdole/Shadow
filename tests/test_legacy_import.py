"""S01: read-only, idempotent legacy import that never invents authorship."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from shadow import legacy_import as LI
from shadow.settings import Settings
from shadow.storage import open_storage

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "legacy_import"
PEOPLE = ["Fictional_Friend", "Old_Imported_Log", "person_1a2b3c4d5e6f"]


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _copy_fixtures(destination: Path) -> Path:
    shutil.copytree(FIXTURES, destination)
    return destination


class ImportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_dir = self.root / "db"
        self.storage = open_storage(self.db_dir)
        self.addCleanup(self.storage.close)
        self.before = _hash_tree(FIXTURES)


class TestImportCounts(ImportCase):
    def test_counts_reconcile(self) -> None:
        report = LI.run_import(FIXTURES, self.storage)
        self.assertEqual(report.folders, 3)
        # A folder with a profile but no chat log is not an imported history.
        (self.root / "empty" / "people" / "Profile_Only").mkdir(parents=True)
        (self.root / "empty" / "people" / "Profile_Only" / "profile.json").write_text("{}")
        self.assertEqual(LI.run_import(self.root / "empty", self.storage).folders, 0)
        self.assertEqual(
            report.rows_imported + report.rows_duplicate + sum(report.quarantined.values()),
            report.rows_total,
        )
        self.assertEqual(report.rows_imported, 28)
        self.assertEqual(report.rows_duplicate, 0)
        self.assertEqual(report.quarantined, {
            "bad_json": 1,
            "missing_sender": 1,
            "bad_sender": 1,
            "missing_content": 2,
        })
        self.assertTrue(report.inputs_unchanged)

    def test_kind_and_sender_classification(self) -> None:
        LI.run_import(FIXTURES, self.storage)
        events = {
            event["seq"]: event
            for event in self.storage.get_legacy_events(
                self._legacy_id("Fictional_Friend")
            )
        }
        self.assertEqual([events[i]["sender"] for i in range(3)],
                         ["contact", "owner", "contact"])
        self.assertEqual(events[6]["kind"], "media")
        self.assertEqual(events[7]["kind"], "reaction")
        self.assertEqual(events[8]["kind"], "system")
        self.assertEqual(events[10]["kind"], "unknown")
        self.assertEqual(events[0]["kind"], "text")

    def test_unicode_and_ordering_survive(self) -> None:
        LI.run_import(FIXTURES, self.storage)
        events = self.storage.get_legacy_events(self._legacy_id("Fictional_Friend"))
        self.assertEqual([e["seq"] for e in events], list(range(len(events))))
        self.assertEqual(events[2]["text"], "😅😅")
        self.assertEqual(events[7]["text"], "Reacted ❤️ to your message")
        self.assertEqual(events[9]["text"], "bhai ek kaam tha, kal bata deta hu")
        self.assertEqual(
            [e["text"] for e in events if e["text"] == "ha"],
            ["ha", "ha", "ha"],
        )

    def test_platform_timestamps_preserved_and_batch_runs_flagged(self) -> None:
        report = LI.run_import(FIXTURES, self.storage)
        events = self.storage.get_legacy_events(self._legacy_id("Fictional_Friend"))
        self.assertEqual(events[0]["platform_ts"], "2024-03-01T09:00:00Z")
        self.assertEqual(events[12]["platform_ts"], "2024-03-01T10:00:00Z")
        self.assertEqual(report.unreliable_timestamp_rows, 3)
        self.assertEqual(report.reliable_timestamp_rows, 25)
        # observed_at is the import time, not a back-dated guess.
        for event in events:
            self.assertNotEqual(event["observed_at"], event["platform_ts"])

    def test_origin_rule(self) -> None:
        report = LI.run_import(FIXTURES, self.storage)
        # Rows the old runtime's own writer could have produced stay unknown.
        runtime_rows = self.storage.get_legacy_events(self._legacy_id("Fictional_Friend"))
        self.assertTrue(all(e["origin"] == "unknown" for e in runtime_rows))
        # A shape the runtime never wrote can be attributed to the owner.
        old_rows = self.storage.get_legacy_events(self._legacy_id("Old_Imported_Log"))
        self.assertEqual(
            [e["origin"] for e in old_rows],
            ["unknown", "owner_authored", "unknown", "owner_authored",
             "unknown", "owner_authored", "unknown", "owner_authored"],
        )
        self.assertEqual(report.origins, {"unknown": 24, "owner_authored": 4})
        self.assertEqual(report.origins.get("shadow_generated", 0), 0)

    def test_nothing_is_bound_to_a_conversation(self) -> None:
        LI.run_import(FIXTURES, self.storage)
        rows = self.storage.conn.execute(
            "SELECT folder_name, binding_status, bound_conversation_id FROM legacy_contacts "
            "ORDER BY folder_name"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], sorted(PEOPLE))
        for row in rows:
            self.assertEqual(row[1], "unbound")
            self.assertIsNone(row[2])
        self.assertEqual(self.storage.table_counts()["conversations"], 0)

    def test_profile_values_reach_the_database(self) -> None:
        LI.run_import(FIXTURES, self.storage)
        contacts = {
            row[0]: (row[1], row[2])
            for row in self.storage.conn.execute(
                "SELECT folder_name, closeness, relationship FROM legacy_contacts"
            )
        }
        self.assertEqual(contacts["Fictional_Friend"], (7, "college friend"))
        self.assertEqual(contacts["Old_Imported_Log"], (4, "batchmate"))
        self.assertEqual(contacts["person_1a2b3c4d5e6f"], (None, None))

    def test_quarantine_stores_no_content(self) -> None:
        LI.run_import(FIXTURES, self.storage)
        rows = self.storage.conn.execute("SELECT * FROM quarantine").fetchall()
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertIn(row["reason"], LI.QUARANTINE_REASONS)
        columns = {row[1] for row in self.storage.conn.execute("PRAGMA table_info(quarantine)")}
        self.assertEqual(columns, {"id", "import_id", "file_hash", "row_index", "reason"})

    def test_imports_record_reconciliation(self) -> None:
        LI.run_import(FIXTURES, self.storage)
        rows = self.storage.conn.execute(
            "SELECT status, SUM(rows_total), SUM(rows_imported), SUM(rows_duplicate), "
            "SUM(rows_quarantined) FROM imports GROUP BY status"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("completed", 33, 28, 0, 5)])

    def _legacy_id(self, folder: str) -> int:
        return self.storage.conn.execute(
            "SELECT id FROM legacy_contacts WHERE folder_name = ?", (folder,)
        ).fetchone()[0]


class TestIdempotenceAndReadOnly(ImportCase):
    def test_second_run_imports_nothing(self) -> None:
        first = LI.run_import(FIXTURES, self.storage)
        second = LI.run_import(FIXTURES, self.storage)
        self.assertEqual(first.rows_imported, 28)
        self.assertEqual(second.rows_imported, 0)
        self.assertEqual(second.rows_duplicate, 28)
        self.assertEqual(second.rows_total, first.rows_total)
        self.assertEqual(self.storage.table_counts()["events"], 28)

    def test_appending_one_line_imports_exactly_one_row(self) -> None:
        source = _copy_fixtures(self.root / "source")
        LI.run_import(source, self.storage)
        log = source / "people" / "Old_Imported_Log" / "chat_log.jsonl"
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"ts": "2023-01-13T09:00:00+00:00", "sender": "them",
                     "content": "ek aur line"},
                    ensure_ascii=False,
                )
                + "\n"
            )
        second = LI.run_import(source, self.storage)
        self.assertEqual(second.rows_imported, 1)
        self.assertEqual(second.rows_duplicate, 28)
        self.assertEqual(second.rows_total, 34)

    def test_input_files_are_never_modified(self) -> None:
        report = LI.run_import(FIXTURES, self.storage)
        self.assertTrue(report.inputs_unchanged)
        self.assertEqual(_hash_tree(FIXTURES), self.before)

    def test_changed_input_aborts(self) -> None:
        source = _copy_fixtures(self.root / "source")
        log = source / "people" / "Old_Imported_Log" / "chat_log.jsonl"
        original = LI._parse_row(log.read_bytes().splitlines()[0])
        # Replace the file between hashing and reading.
        real_hash = LI._sha256
        seen: dict[Path, int] = {}

        def swapping_hash(path: Path) -> str:
            value = real_hash(path)
            seen[path] = seen.get(path, 0) + 1
            if path == log and seen[path] == 1:
                log.write_bytes(
                    log.read_bytes()
                    + b'{"ts": "x", "sender": "them", "content": "late"}\n'
                )
            return value

        LI._sha256 = swapping_hash
        try:
            with self.assertRaises(LI.ImportAborted):
                LI.run_import(source, self.storage)
        finally:
            LI._sha256 = real_hash
        self.assertIsNotNone(original)
        self.assertEqual(self.storage.table_counts()["events"], 0)

    def test_dry_run_uses_a_throwaway_database(self) -> None:
        scratch = self.root / "scratch"
        report = LI.run_import(FIXTURES, None, dry_run=True, scratch_dir=scratch)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.rows_imported, 28)
        self.assertEqual(self.storage.table_counts()["events"], 0)
        self.assertFalse(scratch.exists(), "the dry-run database must be deleted")
        self.assertEqual(_hash_tree(FIXTURES), self.before)

    def test_live_import_without_a_destination_is_refused(self) -> None:
        with self.assertRaises(LI.ImportAborted):
            LI.run_import(FIXTURES, None, dry_run=False)

    def test_missing_source_is_reported(self) -> None:
        with self.assertRaises(LI.ImportAborted):
            LI.run_import(self.root / "nowhere", self.storage)


class TestLatencyProfile(ImportCase):
    def build(self, folders: int, pairs: int, hour: int) -> Path:
        source = self.root / f"generated-{folders}-{pairs}-{hour}"
        base = datetime(2023, 5, 1, hour, 0, tzinfo=timezone.utc)
        for folder in range(folders):
            people = source / "people" / f"Generated_{folder}"
            people.mkdir(parents=True)
            lines = []
            for index in range(pairs):
                asked = base + timedelta(days=index, minutes=folder)
                answered = asked + timedelta(minutes=3 + index % 5)
                lines.append(json.dumps({"ts": asked.isoformat(), "sender": "them",
                                         "content": f"question {index}"}))
                lines.append(json.dumps({"ts": answered.isoformat(), "sender": "me",
                                         "content": f"answer {index}"}))
            (people / "chat_log.jsonl").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
        return source

    def test_profile_absent_with_too_few_samples(self) -> None:
        report = LI.run_import(FIXTURES, self.storage)
        self.assertFalse(report.latency_profile_present)
        self.assertEqual(report.latency_samples, 4)
        self.assertEqual(report.latency_timezone, "UTC")
        self.assertIn("fallback range", report.latency_profile_note)
        self.assertEqual(self.storage.latency_profile(), {})

    def test_profile_present_with_enough_reliable_samples(self) -> None:
        source = self.build(folders=2, pairs=20, hour=10)
        report = LI.run_import(source, self.storage, settings=Settings())
        self.assertTrue(report.latency_profile_present)
        self.assertEqual(report.latency_samples, 40)
        stored = self.storage.latency_profile()
        self.assertEqual(list(stored), ["06-12"])
        self.assertEqual(stored["06-12"]["samples"], 40)
        self.assertLessEqual(stored["06-12"]["p25"], stored["06-12"]["p50"])
        self.assertLessEqual(stored["06-12"]["p50"], stored["06-12"]["p75"])

    def test_owner_timezone_moves_the_bucket(self) -> None:
        source = self.build(folders=2, pairs=20, hour=10)
        report = LI.run_import(
            source, self.storage, settings=Settings(timezone="Asia/Kolkata")
        )
        self.assertTrue(report.latency_profile_present)
        self.assertEqual(report.latency_timezone, "Asia/Kolkata")
        self.assertEqual(list(self.storage.latency_profile()), ["12-18"])

    def test_batch_written_rows_never_become_samples(self) -> None:
        # The old bot appended whole bursts with one timestamp; those rows are
        # both unattributed and time-unreliable, so they cannot be a sample.
        source = self.root / "batched"
        people = source / "people" / "Batched_Folder"
        people.mkdir(parents=True)
        base = datetime(2023, 6, 1, 9, 0, tzinfo=timezone.utc)
        lines = []
        for index in range(40):
            lines.append(json.dumps({"ts": base.isoformat(), "sender": "them",
                                     "content": f"q{index}"}))
            lines.append(json.dumps({"ts": base.isoformat(), "sender": "me",
                                     "content": f"a{index}"}))
        (people / "chat_log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = LI.run_import(source, self.storage, settings=Settings())
        self.assertEqual(report.unreliable_timestamp_rows, 80)
        self.assertEqual(report.latency_samples, 0)
        self.assertFalse(report.latency_profile_present)

    def test_runs_of_two_sharing_a_timestamp_stay_reliable(self) -> None:
        source = self.root / "pairs"
        people = source / "people" / "Two_At_A_Time"
        people.mkdir(parents=True)
        base = datetime(2023, 6, 1, 9, 0, tzinfo=timezone.utc)
        lines = []
        for index in range(40):
            lines.append(json.dumps({"ts": (base + timedelta(minutes=2 * index)).isoformat(),
                                     "sender": "them", "content": f"q{index}"}))
            lines.append(json.dumps({"ts": (base + timedelta(minutes=2 * index + 1)).isoformat(),
                                     "sender": "me", "content": f"a{index}"}))
        (people / "chat_log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = LI.run_import(source, self.storage, settings=Settings())
        self.assertEqual(report.unreliable_timestamp_rows, 0)
        self.assertEqual(report.latency_samples, 40)


@contextmanager
def _forbid(*names: str):
    real = builtins.__import__

    def guard(name, *args, **kwargs):
        if any(name == n or name.startswith(n + ".") for n in names):
            raise AssertionError(f"{name} must not be imported")
        return real(name, *args, **kwargs)

    builtins.__import__ = guard
    try:
        yield
    finally:
        builtins.__import__ = real


class TestCommandLine(ImportCase):
    def setUp(self) -> None:
        super().setUp()
        from shadow.main import cli

        self.cli = cli
        self.runner = CliRunner()

    def test_run_fails_closed_without_touching_the_browser(self) -> None:
        with _forbid("shadow.browser", "shadow.loop", "playwright"):
            result = self.runner.invoke(self.cli, ["run"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Legacy runtime is disabled since P01", result.output)
        self.assertIn("P03", result.output)
        self.assertIn("shadow reply", result.output)

    def test_import_history_needs_dry_run_or_explicit_consent(self) -> None:
        result = self.runner.invoke(
            self.cli, ["import-history", "--source", str(FIXTURES)],
            env={"SHADOW_DATA_DIR": ""},
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("SHADOW_DATA_DIR is not set", result.output)
        self.assertEqual(self.storage.table_counts()["events"], 0)

    def test_import_history_refuses_without_the_backup_flag(self) -> None:
        result = self.runner.invoke(
            self.cli,
            ["import-history", "--source", str(FIXTURES)],
            env={"SHADOW_DATA_DIR": str(self.root / "private")},
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--i-have-a-verified-backup", result.output)
        self.assertFalse((self.root / "private" / "shadow.sqlite3").exists())

    def test_import_history_requires_a_source(self) -> None:
        result = self.runner.invoke(self.cli, ["import-history", "--dry-run"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--source", result.output)

    def test_import_history_dry_run_prints_counts_only(self) -> None:
        private = self.root / "private"
        result = self.runner.invoke(
            self.cli,
            ["import-history", "--source", str(FIXTURES), "--dry-run"],
            env={"SHADOW_DATA_DIR": str(private)},
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("rows imported:           28", result.output)
        self.assertIn("latency profile:         absent", result.output)
        for secret in ("Fictional_Friend", "Old_Imported_Log", "bhai ek kaam"):
            self.assertNotIn(secret, result.output)
        self.assertFalse((private / "shadow.sqlite3").exists())
        self.assertEqual(_hash_tree(FIXTURES), self.before)


if __name__ == "__main__":
    unittest.main()

"""Offline operational paths: no private corpus, credentials or browser needed."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from shadow.coordinator import Coordinator
from shadow.main import cli
from shadow.observe import NoDecisionProvider
from shadow.operations import local_control, private_destination, readiness, start_controls
from shadow.settings import Settings
from shadow.storage import open_storage


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_pause_does_not_need_database_second_writer(self):
        with tempfile.TemporaryDirectory(prefix="sh-ops-") as directory, open_storage(directory) as storage:
            coordinator = Coordinator(storage, None, NoDecisionProvider(), Settings(), account_id="fictional")
            server = await start_controls(directory, coordinator)
            try:
                result = await asyncio.to_thread(local_control, directory, "pause")
                self.assertEqual(result["status"], "paused")
                self.assertTrue(coordinator.paused)
                coordinator.pause("challenge")
                result = await asyncio.to_thread(local_control, directory, "resume")
                self.assertEqual(result["status"], "refused")
            finally:
                server.close()
                await server.wait_closed()

    def test_doctor_redacts_values_and_does_not_create_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "not-created"
            with patch.dict(os.environ, {"SHADOW_DATA_DIR": str(target)}):
                report = readiness({"SHADOW_TELEGRAM_TOKEN": "not-a-real-token", "SHADOW_ACCOUNT_ID": "private-account"})
            self.assertNotIn("private-account", str(report))
            self.assertFalse(target.exists())
            self.assertEqual(report["live_submission"], "locked")

    def test_backup_restore_check_uses_private_new_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private"
            with open_storage(private) as storage:
                storage.get_or_create_conversation("fictional", "123")
            backup = Path(directory) / "backup.sqlite3"
            with patch.dict(os.environ, {"SHADOW_DATA_DIR": str(private)}):
                result = CliRunner().invoke(cli, ["backup", "--output", str(backup)])
                self.assertEqual(result.exit_code, 0, result.output)
                result = CliRunner().invoke(cli, ["backup", "--output", str(backup)])
                self.assertNotEqual(result.exit_code, 0)
                result = CliRunner().invoke(cli, ["restore-check", "--backup", str(backup),
                                                  "--scratch", str(Path(directory) / "restore")])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn('"integrity": "ok"', result.output)


if __name__ == "__main__":
    unittest.main()

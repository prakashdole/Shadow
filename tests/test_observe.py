"""The v2 CLI's bounded observation path never submits a message."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from fake_ports import FakeBrowser, event, read, row
from shadow.contracts import InboxResult
from shadow.main import cli
from shadow.observe import observe_once
from shadow.settings import Settings, SettingsError
from shadow.storage import open_storage


class LifecycleBrowser(FakeBrowser):
    def __init__(self, status="ok"):
        super().__init__()
        self.status = status
        self.closed = False

    async def start(self):
        return InboxResult(status=self.status)

    async def close(self):
        self.closed = True


class TestObservation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="shadow-observe-")
        self.addCleanup(self.scratch.cleanup)
        self.storage = open_storage(Path(self.scratch.name))
        self.addCleanup(self.storage.close)
        self.browser = LifecycleBrowser()

    async def test_baseline_and_new_work_never_send_without_decisions(self):
        self.browser.inbox_result = InboxResult(status="ok", rows=(row(0),))
        self.browser.reads["100200300"] = read(
            "100200300", (event(0, "contact", "first"),)
        )
        first = await observe_once(self.storage, self.browser, Settings(), "test-account")
        self.assertEqual(first.ingested, 1)
        self.assertTrue(self.browser.closed)

        self.browser.reads["100200300"] = read(
            "100200300", (event(0, "contact", "first"),
                           event(1, "contact", "second"))
        )
        await observe_once(self.storage, self.browser, Settings(), "test-account")
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.storage.table_counts()["events"], 2)
        conversation = self.storage.get_conversation_by_thread("test-account", "100200300")
        self.assertEqual(self.storage.get_work(conversation["id"])["held_reason"],
                         "assessment_not_configured")

    async def test_wrong_account_pauses_without_opening_threads(self):
        self.browser.status = "wrong_account"
        result = await observe_once(self.storage, self.browser, Settings(), "test-account")
        self.assertEqual(result.stopped_reason, "wrong_account")
        self.assertEqual(self.browser.calls, [])
        self.assertEqual(self.storage.get_runtime()["pause_reason"], "wrong_account")
        self.assertTrue(self.browser.closed)

    async def test_refuses_a_stored_sending_mode_before_browser_start(self):
        self.storage.set_runtime(account_id="test-account", mode="autonomous",
                                 autonomy_scope="known", pause_reason=None,
                                 config_digest=None)
        with self.assertRaises(SettingsError):
            await observe_once(self.storage, self.browser, Settings(), "test-account")
        self.assertFalse(self.browser.closed)


class TestObserveCLI(unittest.TestCase):
    def test_requires_explicit_private_account_binding(self):
        with patch.dict(os.environ, {"SHADOW_ACCOUNT_ID": "",
                                     "SHADOW_EXPECTED_ACCOUNT": ""}):
            result = CliRunner().invoke(cli, ["observe-once"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("SHADOW_ACCOUNT_ID", result.output)

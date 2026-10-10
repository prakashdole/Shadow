"""Fictional, real-SQLite checks for the controlled one-message test harness."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from fake_ports import FakeBrowser, SendScript, event, read
from shadow.contracts import SendResult
from shadow.settings import Settings
from shadow.storage import StorageLocked, open_storage
from tools.live_send import controlled_send

THREAD = "100200300"
ACCOUNT = EXPECTED = "fictional_owner"


def message() -> str:
    return f"[SHADOW CONTROLLED TEST {uuid.uuid4()}] fictional delivery check"


def settings() -> Settings:
    return Settings(mode="assisted", timezone="UTC", active_hours={"daily": ("00:00", "23:59")})


class _Page:
    url = f"https://www.instagram.com/direct/t/{THREAD}/"


class ControlledSendTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, browser, *, thread=THREAD, text=None):
        with tempfile.TemporaryDirectory() as directory:
            return await controlled_send(
                browser, state_dir=Path(directory) / "new-private-state",
                account_id=ACCOUNT, expected_account=EXPECTED, thread_id=thread,
                text=text or message(), settings=settings(), timeout_seconds=5,
            )

    @staticmethod
    def healthy_browser():
        browser = FakeBrowser(reads={THREAD: read(THREAD, (event(0, "contact", "fictional inbound"),))})
        browser.page = _Page()
        browser._expected_account = EXPECTED
        return browser

    async def test_one_healthy_owner_directed_message_uses_coordinator_chain(self):
        browser = self.healthy_browser()
        result = await self.invoke(browser)
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.outbox_state, "confirmed")
        self.assertEqual(result.attempts, 1)
        self.assertIsNotNone(result.confirmed_event_id)
        self.assertEqual(browser.sent and len(browser.sent), 1)
        self.assertEqual(browser.sent[0][0], THREAD)
        self.assertEqual(browser.check_points, ["before_type", "before_enter"])

    async def test_wrong_account_thread_manual_composer_and_receipt_never_send(self):
        cases = (
            ("wrong_account", read(THREAD, status="wrong_account"), "wrong_account"),
            ("wrong_thread", read("999", (event(0, "contact", "fictional inbound"),)), "wrong_thread"),
            ("manual", read(THREAD, (event(0, "contact", "fictional inbound"),), composer_text="owner typing"), "manual_text"),
            ("receipt", read(THREAD, status="receipts_visible"), "receipts_visible"),
        )
        for name, result, reason in cases:
            with self.subTest(name=name):
                browser = FakeBrowser(reads={THREAD: result})
                browser.page = _Page()
                browser._expected_account = EXPECTED
                outcome = await self.invoke(browser)
                self.assertEqual((outcome.status, outcome.reason), ("refused", reason))
                self.assertEqual(browser.sent, [])

    async def test_uncertain_attempt_is_persisted_and_never_retried(self):
        browser = self.healthy_browser()
        browser.queue(SendResult(status="uncertain", thread_id=THREAD, error_code="no_confirmation"))
        result = await self.invoke(browser)
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.outbox_state, "uncertain")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(browser.sent), 1)

    async def test_existing_state_directory_is_refused_before_browser_read(self):
        browser = self.healthy_browser()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                await controlled_send(
                    browser, state_dir=Path(directory), account_id=ACCOUNT,
                    expected_account=EXPECTED, thread_id=THREAD,
                    text=message(), settings=settings(), timeout_seconds=5,
                )
        self.assertEqual(browser.calls, [])

    async def test_adapter_without_the_exact_expected_account_binding_never_reads_or_sends(self):
        browser = self.healthy_browser()
        browser._expected_account = "different_fictional_owner"
        result = await self.invoke(browser)
        self.assertEqual((result.status, result.reason), ("refused", "expected_account_not_bound"))
        self.assertEqual(browser.calls, [])

    async def test_missing_none_and_mismatched_account_binding_never_reads(self):
        for binding in ("missing", None, "different_fictional_owner"):
            with self.subTest(binding=binding):
                browser = self.healthy_browser()
                if binding == "missing":
                    del browser._expected_account
                else:
                    browser._expected_account = binding
                result = await self.invoke(browser)
                self.assertEqual(result.reason, "expected_account_not_bound")
                self.assertEqual(browser.calls, [])

    async def test_current_route_and_any_read_navigation_refuse_without_send(self):
        browser = self.healthy_browser()
        browser.page.url = "https://www.instagram.com/direct/inbox/"
        result = await self.invoke(browser)
        self.assertEqual((result.status, result.reason), ("refused", "current_thread_not_open"))
        self.assertEqual(browser.sent, [])

        browser = self.healthy_browser()
        browser.reads[THREAD] = read(THREAD, (event(0, "contact", "fictional inbound"),))
        original = browser.read_thread

        async def navigated(thread_id, want=0, *, anchor=()):
            return replace(await original(thread_id, want, anchor=anchor), navigations_used=1)

        browser.read_thread = navigated
        result = await self.invoke(browser)
        self.assertEqual((result.status, result.reason), ("refused", "navigation_detected"))
        self.assertEqual(browser.sent, [])

    async def test_route_change_after_baseline_blocks_the_second_inner_read(self):
        browser = self.healthy_browser()
        original = browser.read_thread
        calls = 0

        async def changes_route_after_baseline(thread_id, want=0, *, anchor=()):
            nonlocal calls
            calls += 1
            result = await original(thread_id, want, anchor=anchor)
            if calls == 1:
                browser.page.url = "https://www.instagram.com/direct/inbox/"
            return result

        browser.read_thread = changes_route_after_baseline
        result = await self.invoke(browser)
        self.assertEqual((result.status, result.reason), ("refused", "navigation_detected"))
        self.assertEqual(calls, 1)
        self.assertEqual(browser.sent, [])

    async def test_state_is_created_and_locked_before_first_read(self):
        browser = self.healthy_browser()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "new-private-state"
            original = browser.read_thread

            async def race_read(thread_id, want=0, *, anchor=()):
                with self.assertRaises(StorageLocked):
                    open_storage(state)
                return await original(thread_id, want, anchor=anchor)

            browser.read_thread = race_read
            result = await controlled_send(
                browser, state_dir=state, account_id=ACCOUNT, expected_account=EXPECTED,
                thread_id=THREAD, text=message(), settings=settings(), timeout_seconds=5,
            )
            self.assertEqual(result.status, "confirmed")

    async def test_timeout_preserves_the_attempting_row_without_retry(self):
        browser = self.healthy_browser()
        browser.send_results.append(SendScript(
            SendResult(status="confirmed", thread_id=THREAD), delay=1,
        ))
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "new-private-state"
            with self.assertRaises(asyncio.TimeoutError):
                await controlled_send(
                    browser, state_dir=state, account_id=ACCOUNT, expected_account=EXPECTED,
                    thread_id=THREAD, text=message(), settings=settings(), timeout_seconds=0.01,
                )
            with open_storage(state) as storage:
                item = storage.outbox_items()[0]
                self.assertEqual((item["state"], item["attempts"]), ("attempting", 1))
        self.assertEqual(len(browser.sent), 1)

    async def test_timeout_must_be_finite_and_positive(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                browser = self.healthy_browser()
                with self.assertRaises(ValueError):
                    await self.invoke_with_timeout(browser, timeout)
                self.assertEqual(browser.calls, [])
        self.assertEqual((await self.invoke_with_timeout(self.healthy_browser(), 1)).status, "confirmed")

    async def invoke_with_timeout(self, browser, timeout):
        with tempfile.TemporaryDirectory() as directory:
            return await controlled_send(
                browser, state_dir=Path(directory) / "new-private-state",
                account_id=ACCOUNT, expected_account=EXPECTED, thread_id=THREAD,
                text=message(), settings=settings(), timeout_seconds=timeout,
            )

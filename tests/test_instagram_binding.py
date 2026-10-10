"""Account identity must remain bound throughout a browser operation."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from shadow.instagram import IG_INBOX, InstagramBrowser


class AccountPage:
    url = "https://www.instagram.com/direct/t/12345/"

    def __init__(self, *handles):
        self.handles = iter(handles)

    async def evaluate(self, _script):
        from shadow.dom import JS_RAW
        if _script == JS_RAW:
            return {"path": "/direct/t/12345/"}
        return next(self.handles)


class _OwnedPage:
    def __init__(self, error=None, url="about:blank"):
        self.error = error
        self.url = url

    async def route(self, *_args):
        pass

    def on(self, *_args):
        pass

    async def goto(self, *_args, **_kwargs):
        if self.error:
            raise self.error


class _OwnedContext:
    def __init__(self, page, close_error=None):
        self.pages = []
        self.page = page
        self.close_error = close_error
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class _OwnedPlaywright:
    def __init__(self, launch_error=None, page_error=None, context_close_error=None):
        self.context = _OwnedContext(_OwnedPage(page_error), context_close_error)
        self.launch_error = launch_error
        self.stopped = False
        self.chromium = self

    async def launch_persistent_context(self, *_args, **_kwargs):
        if self.launch_error:
            raise self.launch_error
        return self.context

    async def stop(self):
        self.stopped = True


class _OwnedPlaywrightFactory:
    def __init__(self, playwright=None, error=None):
        self.playwright = playwright
        self.error = error

    async def start(self):
        if self.error:
            raise self.error
        return self.playwright


class _AttachedBrowser:
    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1


class TestAccountBinding(unittest.IsolatedAsyncioTestCase):
    def test_headless_requires_a_private_dedicated_profile(self):
        import tempfile
        from pathlib import Path

        with self.assertRaises(ValueError):
            InstagramBrowser(headless=True)
        with tempfile.TemporaryDirectory() as directory:
            browser = InstagramBrowser(profile_dir=Path(directory) / "shadow-profile", headless=True)
            self.assertTrue(browser.headless)
            self.assertIsNotNone(browser.profile_dir)
            with self.assertRaises(ValueError):
                InstagramBrowser(profile_dir=Path(directory) / "Default", headless=True)

    async def test_account_switch_after_initial_binding_is_detected(self):
        browser = InstagramBrowser(expected_account="fictional_owner")
        browser.page = AccountPage("fictional_owner", "fictional_other")
        self.assertIsNone(await browser._check_account())
        result = await browser._check_account()
        self.assertEqual(result.status, "wrong_account")
        self.assertEqual(result.detail, "account_handle_differs")

    async def test_unreadable_identity_fails_closed(self):
        browser = InstagramBrowser(expected_account="fictional_owner")
        browser.page = AccountPage(None)
        result = await browser._check_account()
        self.assertEqual((result.status, result.detail),
                         ("wrong_account", "account_unverified"))

    async def test_account_switch_before_enter_blocks_submission(self):
        browser = InstagramBrowser(expected_account="fictional_owner")
        browser.page = AccountPage(
            "fictional_owner", "fictional_owner", "fictional_other"
        )

        async def send_stub(_page, _thread_id, _text, check):
            self.assertTrue(await check("before_type"))
            self.assertFalse(await check("before_enter"))
            return None

        async def allow(_point):
            return True

        with patch("shadow.instagram._send_on_page", new=send_stub):
            await browser.send_one("12345", "not sent", allow)

    async def test_login_and_challenge_take_precedence_over_missing_account(self):
        class ScreenPage(AccountPage):
            path = "/accounts/login/"
            async def evaluate(inner, _script):
                return {"path": inner.path}
        browser = InstagramBrowser(expected_account="fictional_owner")
        browser.page = ScreenPage()
        self.assertEqual((await browser._check_account()).status, "login_required")
        browser.page.path = "/challenge/"
        self.assertEqual((await browser._check_account()).status, "challenge")

    async def test_owned_startup_failure_details_are_coarse_and_cleanup(self):
        cases = (
            (RuntimeError("launch secret=top-secret"), None,
             "owned_browser_launch_failed"),
            (None, asyncio.TimeoutError("https://www.instagram.com/private-token"),
             "owned_browser_navigation_timeout"),
            (None, RuntimeError("net::ERR_NAME_NOT_RESOLVED secret=top-secret"),
             "owned_browser_navigation_dns_failed"),
            (None, RuntimeError("private-token timeout=top-secret"),
             "owned_browser_start_failed"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for launch_error, page_error, detail in cases:
                with self.subTest(detail=detail):
                    playwright = _OwnedPlaywright(launch_error, page_error)
                    browser = InstagramBrowser(profile_dir=Path(directory) / detail,
                                               headless=True)
                    with patch("shadow.instagram.async_playwright",
                               return_value=_OwnedPlaywrightFactory(playwright)):
                        result = await browser.start()
                    self.assertEqual((result.status, result.detail), ("dom_unknown", detail))
                    self.assertNotIn("secret", result.detail)
                    self.assertNotIn("private-token", result.detail)
                    self.assertTrue(playwright.stopped)
                    if not launch_error:
                        self.assertTrue(playwright.context.closed)

    async def test_driver_start_failure_returns_a_safe_status_without_state(self):
        browser = InstagramBrowser()
        with patch("shadow.instagram.async_playwright", return_value=_OwnedPlaywrightFactory(
                error=RuntimeError("private-token=top-secret"))):
            result = await browser.start()
        self.assertEqual((result.status, result.detail),
                         ("dom_unknown", "browser_driver_start_failed"))
        self.assertIsNone(browser._pw)
        self.assertIsNone(browser.page)

    async def test_owned_readiness_failure_closes_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            playwright = _OwnedPlaywright()
            browser = InstagramBrowser(profile_dir=Path(directory) / "profile", headless=True)
            with (patch("shadow.instagram.async_playwright",
                        return_value=_OwnedPlaywrightFactory(playwright)),
                  patch.object(browser, "_wait_readable", new=AsyncMock(return_value=False))):
                result = await browser.start()
        self.assertEqual((result.status, result.detail),
                         ("dom_unknown", "owned_browser_readiness_failed"))
        self.assertTrue(playwright.context.closed)
        self.assertTrue(playwright.stopped)
        self.assertIsNone(browser.page)

    async def test_owned_multiple_tabs_failure_closes_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            playwright = _OwnedPlaywright()
            playwright.context.pages = [_OwnedPage(url=IG_INBOX), _OwnedPage(url=IG_INBOX)]
            browser = InstagramBrowser(profile_dir=Path(directory) / "profile", headless=True)
            with patch("shadow.instagram.async_playwright",
                       return_value=_OwnedPlaywrightFactory(playwright)):
                result = await browser.start()
        self.assertEqual((result.status, result.detail),
                         ("dom_unknown", "owned_profile_multiple_instagram_tabs"))
        self.assertTrue(playwright.context.closed)
        self.assertTrue(playwright.stopped)
        self.assertIsNone(browser.page)

    async def test_owned_cleanup_failure_still_stops_driver_and_returns_safe_status(self):
        with tempfile.TemporaryDirectory() as directory:
            playwright = _OwnedPlaywright(
                page_error=RuntimeError("private-token=top-secret"),
                context_close_error=RuntimeError("context close failed"),
            )
            browser = InstagramBrowser(profile_dir=Path(directory) / "profile", headless=True)
            with patch("shadow.instagram.async_playwright",
                       return_value=_OwnedPlaywrightFactory(playwright)):
                result = await browser.start()
        self.assertEqual((result.status, result.detail),
                         ("dom_unknown", "owned_browser_start_failed"))
        self.assertTrue(playwright.context.closed)
        self.assertTrue(playwright.stopped)
        self.assertIsNone(browser._pw)
        self.assertIsNone(browser._owned_context)
        self.assertIsNone(browser.page)

    async def test_attached_browser_cleanup_only_stops_playwright(self):
        browser = InstagramBrowser()
        playwright = _OwnedPlaywright()
        attached = _AttachedBrowser()
        browser._pw = playwright
        browser._browser = attached
        browser.page = object()
        await browser.close()
        self.assertTrue(playwright.stopped)
        self.assertEqual(attached.close_calls, 0)
        self.assertIsNone(browser._browser)
        self.assertIsNone(browser.page)

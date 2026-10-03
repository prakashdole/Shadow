"""Account identity must remain bound throughout a browser operation."""

import unittest
from unittest.mock import patch

from shadow.instagram import InstagramBrowser


class AccountPage:
    url = "https://www.instagram.com/direct/t/12345/"

    def __init__(self, *handles):
        self.handles = iter(handles)

    async def evaluate(self, _script):
        return next(self.handles)


class TestAccountBinding(unittest.IsolatedAsyncioTestCase):
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

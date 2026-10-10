"""Fictional authenticated-navigation shapes for account binding."""

from __future__ import annotations

import unittest

from shadow.dom import JS_RAW
from shadow.instagram import InstagramBrowser, _JS_NAV_HANDLE


class _BindingPage:
    """Give the production account guard a fictional browser page."""

    url = "https://www.instagram.com/direct/inbox/"

    def __init__(self, page):
        self._page = page

    async def evaluate(self, script):
        if script == JS_RAW:
            return {"path": "/direct/inbox/"}
        return await self._page.evaluate(script)


class AccountNavigationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch()
        except Exception as exc:
            self.skipTest(f"headless Chromium unavailable: {type(exc).__name__}: {str(exc)[:160]}")
        self.page = await self._browser.new_page(viewport={"width": 1280, "height": 900})

    async def asyncTearDown(self) -> None:
        await self.page.close()
        await self._browser.close()
        await self._playwright.stop()

    async def content(self, body: str) -> None:
        await self.page.set_content(f"""
            <style>
              .rail {{ position: fixed; left: 12px; top: 520px; width: 48px; height: 56px; }}
              .avatar {{ width: 48px; height: 48px; }}
              .app-navigation {{ position: fixed; left: 12px; top: 72px; width: 48px; height: 692px; }}
              .wide-shell {{ width: 1000px; height: 800px; }}
            </style>
            {body}
        """)

    @staticmethod
    def application_navigation(body: str) -> str:
        return f'''<div class="app-navigation"><a href="/">Home</a>
            <a href="/direct/inbox/">Inbox</a>{body}</div>'''

    async def test_compact_rail_avatar_binds_the_owner_handle(self) -> None:
        await self.content(self.application_navigation("""
            <div class="rail"><a href="/fictional_owner/"><img class="avatar"
                alt="fictional_owner's profile picture"></a></div>
        """))
        self.assertEqual(await self.page.evaluate(_JS_NAV_HANDLE), "fictional_owner")

    async def test_wrong_account_still_fails_the_production_guard(self) -> None:
        await self.content(self.application_navigation("""
            <div class="rail"><a href="/fictional_other/"><img class="avatar"
                alt="fictional_other's profile picture"></a></div>
        """))
        browser = InstagramBrowser(expected_account="fictional_owner")
        browser.page = _BindingPage(self.page)
        result = await browser._check_account()
        self.assertEqual((result.status, result.detail), ("wrong_account", "account_handle_differs"))

    async def test_multiple_bound_avatars_hold_identity_unknown(self) -> None:
        await self.content(self.application_navigation("""
            <div class="rail"><a href="/fictional_one/"><img class="avatar"
                alt="fictional_one's profile picture"></a></div>
            <div class="rail" style="top:600px"><a href="/fictional_two/"><img class="avatar"
                alt="fictional_two's profile picture"></a></div>
        """))
        self.assertIsNone(await self.page.evaluate(_JS_NAV_HANDLE))

    async def test_profile_link_inside_chat_is_not_authenticated_navigation(self) -> None:
        await self.content("""
            <main><div role="article"><a href="/fictional_contact/"><img class="avatar"
                alt="fictional_contact's profile picture"></a></div></main>
        """)
        self.assertIsNone(await self.page.evaluate(_JS_NAV_HANDLE))

    async def test_unrelated_left_aside_is_not_authenticated_navigation(self) -> None:
        await self.content("""
            <aside aria-label="Suggested accounts" class="rail"><a href="/fictional_owner/"><img class="avatar"
                alt="fictional_owner's profile picture"></a></aside>
        """)
        self.assertIsNone(await self.page.evaluate(_JS_NAV_HANDLE))

    async def test_suggested_aside_inside_the_app_shell_is_not_navigation(self) -> None:
        await self.content("""
            <div class="wide-shell"><nav><a href="/">Home</a><a href="/direct/inbox/">Inbox</a></nav>
              <aside aria-label="Suggested accounts" class="rail"><a href="/fictional_owner/"><img class="avatar"
                  alt="fictional_owner's profile picture"></a></aside>
            </div>
        """)
        self.assertIsNone(await self.page.evaluate(_JS_NAV_HANDLE))

    async def test_suggested_section_in_a_wide_common_shell_is_not_navigation(self) -> None:
        await self.content("""
            <div class="wide-shell"><nav><a href="/">Home</a><a href="/direct/inbox/">Inbox</a></nav>
              <section aria-label="Suggested accounts" class="rail"><a href="/fictional_owner/"><img class="avatar"
                  alt="fictional_owner's profile picture"></a></section>
            </div>
        """)
        self.assertIsNone(await self.page.evaluate(_JS_NAV_HANDLE))

    async def test_avatar_alt_must_exactly_bind_the_profile_handle(self) -> None:
        await self.content(self.application_navigation("""
            <div class="rail"><a href="/fictional_owner/"><img class="avatar"
                alt="not_fictional_owner's profile picture"></a></div>
        """))
        self.assertIsNone(await self.page.evaluate(_JS_NAV_HANDLE))

    async def test_hidden_avatar_is_not_authenticated_navigation(self) -> None:
        await self.content(self.application_navigation("""
            <div class="rail"><a href="/fictional_owner/" style="display:block;width:48px;height:48px"><img class="avatar"
                style="display:none" alt="fictional_owner's profile picture"></a></div>
        """))
        self.assertIsNone(await self.page.evaluate(_JS_NAV_HANDLE))

    async def test_explicit_navigation_profile_link_remains_supported(self) -> None:
        await self.content("""
            <nav><a href="/fictional_owner/" aria-label="Profile">Profile</a></nav>
        """)
        self.assertEqual(await self.page.evaluate(_JS_NAV_HANDLE), "fictional_owner")

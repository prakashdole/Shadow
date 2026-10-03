"""tests/browser/ — the production reader on a real headless Chromium.

No __init__.py, matching the offline suite. Every fixture is fictional and
loaded from file://. If Chromium cannot launch, the whole module skips with the
reason; it never passes silently.

    python3 -m unittest discover -s tests/browser -p 'test_*.py' -v
"""

from __future__ import annotations

import asyncio
import functools
import unittest
from pathlib import Path

from shadow.dom import JS_RAW, interpret_inbox, interpret_read, normalize
from shadow.instagram import _send_on_page, refuse_cdp_url

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "dom"

# Long enough for a delayed bubble, short enough that a failure is quick.
CONFIRM_TIMEOUT = 4.0


def url(name: str) -> str:
    return (FIXTURES / name).as_uri()


async def _new_browser():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch()
    except Exception:
        await pw.stop()
        raise
    return pw, browser


class BrowserCase(unittest.IsolatedAsyncioTestCase):
    """One headless Chromium per test, one fixture page per test."""

    async def asyncSetUp(self) -> None:
        try:
            self._pw, self._browser = await _new_browser()
        except Exception as exc:
            self.skipTest(
                f"headless Chromium unavailable: {type(exc).__name__}: "
                f"{str(exc)[:160]}"
            )
        self.page = None

    async def asyncTearDown(self) -> None:
        if self.page is not None:
            await self.page.close()
        if getattr(self, "_browser", None):
            await self._browser.close()
        if getattr(self, "_pw", None):
            await self._pw.stop()

    async def open_fixture(self, name: str, path: str | None = None) -> None:
        self.page = await self._browser.new_page()
        await self.page.goto(url(name) + (f"#{path}" if path else ""),
                             wait_until="load")
        await self.page.wait_for_timeout(80)

    async def set_path(self, path: str) -> None:
        await self.page.evaluate("(p) => { window.__fixturePath = p; }", path)

    async def read(self, thread_id: str = "12345"):
        return interpret_read(await self.page.evaluate(JS_RAW), thread_id)

    async def inbox(self):
        return interpret_inbox(await self.page.evaluate(JS_RAW))


class B01StatusTest(BrowserCase):
    """B01: a missing structure is dom_unknown; a verified empty UI is empty."""

    async def test_missing_thread_list_is_dom_unknown_not_empty(self) -> None:
        await self.open_fixture("screen_login.html")
        result = await self.inbox()
        self.assertEqual(result.status, "dom_unknown")
        self.assertEqual(result.rows, ())

    async def test_rendered_list_with_no_rows_is_verified_empty(self) -> None:
        await self.open_fixture("inbox_empty.html")
        result = await self.inbox()
        self.assertEqual(result.status, "empty_verified")
        self.assertEqual(result.rows, ())

    async def test_missing_pane_is_dom_unknown_never_an_empty_thread(self) -> None:
        await self.open_fixture("thread_no_pane.html")
        result = await self.read()
        self.assertEqual(result.status, "dom_unknown")
        self.assertEqual(result.events, ())
        self.assertFalse(result.complete)

    async def test_a_thread_that_does_render_reads_ok(self) -> None:
        await self.open_fixture("thread_1to1.html")
        result = await self.read()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.events)
        self.assertTrue(result.complete)


class B02IdentityTest(BrowserCase):
    """B02: a label is not an identity. Duplicate labels and id prefixes hold."""

    async def test_duplicate_display_labels_are_reported_not_guessed(self) -> None:
        await self.open_fixture("inbox_duplicate_labels.html")
        result = await self.inbox()
        self.assertEqual(result.status, "ok")
        duplicated = [r for r in result.rows if r.label == "Test Contact Ten"]
        self.assertEqual(len(duplicated), 2)
        for row in duplicated:
            self.assertFalse(row.label_is_unique)
        self.assertTrue(next(r for r in result.rows
                             if r.label == "Test Contact Eleven").label_is_unique)

    async def test_open_row_refuses_a_label_that_is_not_unique(self) -> None:
        from shadow.contracts import InboxRow
        from shadow.instagram import InstagramBrowser

        await self.open_fixture("inbox_duplicate_labels.html")
        adapter = InstagramBrowser()
        adapter.page = self.page
        result = await adapter.open_row(InboxRow(
            row_index=0, label="Test Contact Ten", preview_text="hello",
            preview_class="text_in", unread_hint=True, label_is_unique=False,
        ))
        self.assertEqual(result.status, "ambiguous_identity")
        self.assertEqual(result.navigations_used, 0, "a hold must not navigate")

    async def test_id_prefix_collision_is_not_equality(self) -> None:
        """12 and 12345 both appear as substrings of one another; they are not."""
        from shadow.dom import same_thread, thread_id_from_url

        page_url = "https://www.instagram.com/direct/t/12345/"
        self.assertEqual(thread_id_from_url(page_url), "12345")
        self.assertIn("12", page_url, "substring containment is the old bug")
        self.assertFalse(same_thread(thread_id_from_url(page_url), "12"))
        self.assertTrue(same_thread(thread_id_from_url(page_url), "12345"))

    async def test_click_that_leaves_previous_thread_open_is_not_success(self):
        from shadow.contracts import InboxRow
        from shadow.instagram import InstagramBrowser
        from unittest.mock import patch
        import json

        await self.open_fixture('thread_1to1.html')
        class FixturePage:
            url = 'https://www.instagram.com/direct/t/12345/'
            async def evaluate(inner, script, *args):
                return await self.page.evaluate(script, *args)
            async def wait_for_function(inner, script, **kwargs):
                script = script.replace('location.pathname', json.dumps('/direct/t/12345/'))
                return await self.page.wait_for_function(script, timeout=120, **{
                    k: v for k, v in kwargs.items() if k != 'timeout'})
        adapter = InstagramBrowser()
        adapter.page = FixturePage()
        row = InboxRow(0, 'Test Contact One', 'You: chalo', 'owner_last', False, True)
        # The rendered row is not selected; its click does nothing.
        await self.page.evaluate("document.querySelector('.row').setAttribute('aria-pressed', 'false')")
        with patch.object(adapter, '_assert_on_site'):
            result = await adapter.open_row(row)
        self.assertEqual(result.status, 'dom_unknown')
        self.assertEqual(result.detail, 'row_did_not_open_a_thread')

    async def test_online_badge_is_not_a_contact_name_or_inbound_preview(self):
        from shadow.instagram import _JS_CLICK_ROW

        await self.open_fixture('inbox.html')
        await self.page.evaluate("""() => {
            const row = document.querySelector('.row');
            row.querySelector('.avatar').innerHTML = '<div>Active</div>';
            row.querySelector('.name').setAttribute('dir', 'auto');
            row.querySelector('.preview').setAttribute('dir', 'auto');
            row.querySelector('.preview').textContent = 'You sent an attachment. · 5m';
        }""")
        row = (await self.inbox()).rows[0]
        self.assertEqual(row.label, 'Test Contact One')
        self.assertEqual(row.preview_class, 'owner_last')
        self.assertFalse(row.unread_hint)
        self.assertEqual(await self.page.evaluate(_JS_CLICK_ROW, row.label), 1)

    async def test_send_one_refuses_a_wrong_thread_id_without_typing(self) -> None:
        from shadow.instagram import InstagramBrowser

        await self.open_fixture("thread_send.html", "append_owner")
        adapter = InstagramBrowser()
        adapter.page = self.page
        result = await adapter.send_one(
            "12345", "must not be typed", self._always
        )
        self.assertEqual(result.status, "failed_before_submit")
        self.assertEqual(result.error_code, "off_site",
                         "a file:// page must never be typed into")
        self.assertEqual(await self._composer_text(), "")

    async def test_missing_participant_evidence_is_reported_not_guessed(self) -> None:
        await self.open_fixture("thread_group_uncertain.html")
        result = await self.read()
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.is_group)
        self.assertEqual(result.participant_links, 1)

    @staticmethod
    async def _always(_point: str) -> bool:
        return True

    async def _composer_text(self) -> str:
        return await self.page.evaluate(
            """() => {
                const b = document.querySelector('div[contenteditable="true"]'
                                                 + '[aria-label="Message"]');
                return b ? (b.innerText || '').trim() : null;
            }"""
        )


class B03KindsTest(BrowserCase):
    """B03: direction, multiline, emoji, quotes, edits, system, media."""

    async def test_settled_current_thread_reads_without_navigation_or_readiness_delay(self):
        from shadow.instagram import InstagramBrowser
        from unittest.mock import AsyncMock, patch

        await self.open_fixture('thread_1to1.html')
        adapter = InstagramBrowser()
        adapter.page = self.page
        await self.set_path('/direct/t/12345/')
        # read_thread still checks URL identity; the fixture supplies that URL.
        class FixturePage:
            url = 'https://www.instagram.com/direct/t/12345/'
            async def evaluate(inner, script):
                return await self.page.evaluate(script)
            async def wait_for_function(inner, script, **kwargs):
                return None
        adapter.page = FixturePage()
        with patch.object(adapter, '_wait_readable', new_callable=AsyncMock) as wait:
            result = await adapter.read_thread('12345')
            wait.assert_not_awaited()
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.navigations_used, 0)

    async def test_adjacent_live_quote_belongs_to_reply_not_a_standalone_message(self):
        await self.open_fixture('thread_1to1.html')
        await self.page.evaluate("""() => {
            const wrap = document.createElement('div');
            wrap.innerHTML = '<div><span dir="auto">Test Contact replied to you</span></div>'
              + '<div><div role="button"><svg aria-label="Clip"><title>Clip</title></svg></div></div>'
              + '<div><div role="article"><span dir="auto">reply text</span></div></div>';
            document.querySelector('.pane').prepend(wrap);
        }""")
        result = await self.read()
        self.assertEqual(result.events[0].text, 'reply text')
        self.assertEqual(result.events[0].reply_to_text, '[quoted reel]')

    async def test_relative_age_seen_receipt_is_a_pause_not_system_text(self):
        await self.open_fixture('thread_1to1.html')
        await self.page.evaluate("""() => {
            const seen = document.createElement('div');
            seen.setAttribute('dir', 'auto'); seen.textContent = 'Seen 1h ago';
            document.querySelector('.pane').append(seen);
        }""")
        self.assertEqual((await self.read()).status, 'receipts_visible')

    async def test_large_emoji_only_bubble_without_dir_auto_is_read(self):
        await self.open_fixture("thread_1to1.html")
        await self.page.evaluate("""() => {
            const art = document.createElement('div');
            art.setAttribute('role', 'article'); art.setAttribute('aria-labelledby', 'large-emoji');
            art.innerHTML = '<div role="none" id="large-emoji"><span>🥀🥀</span></div>';
            document.querySelector('.pane').prepend(art);
        }""")
        self.assertEqual((await self.read()).events[0].text, '🥀🥀')

    async def test_live_collage_post_is_media_not_authored_caption(self):
        await self.open_fixture('thread_1to1.html')
        await self.page.evaluate("""() => {
            const art = document.createElement('div'); art.setAttribute('role', 'article');
            art.innerHTML = '<a href="/fictional_creator/" dir="auto">creator</a>'
              + '<div role="button"><img alt=""><svg aria-label="Collage"><title>Collage</title></svg>'
              + '<span dir="auto">creator A third-party caption, not the sender’s words.</span></div>';
            document.querySelector('.pane').prepend(art);
        }""")
        event = (await self.read()).events[0]
        self.assertEqual((event.kind, event.media_label), ('media', 'post'))

    async def test_live_reel_icon_and_creator_links_do_not_become_text_or_participants(self):
        await self.open_fixture("thread_1to1.html")
        await self.page.evaluate("""() => {
            const pane = document.querySelector('.pane');
            document.querySelector('.headlink').append(document.createElement('img'));
            const art = document.createElement('div');
            art.setAttribute('role', 'article');
            art.innerHTML = '<div role="button"><a href="/fictional_creator/" dir="auto">creator</a>'
                + '<svg aria-label="Clip"><title>Clip</title></svg></div>';
            pane.prepend(art);
        }""")
        result = await self.read()
        self.assertEqual(result.events[0].kind, "media")
        self.assertEqual(result.events[0].media_label, "reel")
        self.assertEqual(result.participant_links, 2)
        self.assertFalse(result.is_group)

    async def test_reverse_scrolling_never_claims_latest_window_is_complete(self):
        await self.open_fixture("thread_1to1.html")
        await self.page.evaluate("""() => {
            const pane = document.querySelector('.pane');
            pane.style.display = 'flex'; pane.style.flexDirection = 'column-reverse';
            pane.scrollTop = 0;
        }""")
        self.assertFalse((await self.read()).complete)

    async def test_readiness_waits_for_delayed_articles_not_only_url(self):
        from shadow.instagram import InstagramBrowser
        from unittest.mock import patch

        await self.open_fixture("thread_1to1.html")
        adapter = InstagramBrowser()
        adapter.page = self.page
        await self.page.evaluate("""() => {
            const pane = document.querySelector('.pane');
            const content = pane.innerHTML; pane.innerHTML = '';
            setTimeout(() => { pane.innerHTML = content; }, 400);
        }""")
        # The read-only fixture is file://; production retains its host guard.
        with patch.object(adapter, '_assert_on_site'):
            self.assertTrue(await adapter._wait_readable(thread=True))
        self.assertTrue((await self.read()).events)

    async def test_direction_is_geometry(self) -> None:
        await self.open_fixture("thread_1to1.html")
        result = await self.read()
        by_text = {normalize(e.text): e.sender for e in result.events
                   if e.kind == "text"}
        self.assertEqual(by_text["kal milte hai?"], "contact")
        self.assertEqual(by_text["haan, 7 pm?"], "owner")

    async def test_two_identical_messages_are_two_events(self) -> None:
        await self.open_fixture("thread_1to1.html")
        oks = [e for e in (await self.read()).events if normalize(e.text) == "ok"]
        self.assertEqual(len(oks), 2, "repeated text must not collapse (D32)")
        self.assertEqual([e.position for e in oks], sorted(e.position for e in oks))

    async def test_emoji_alt_text_becomes_message_text(self) -> None:
        await self.open_fixture("thread_1to1.html")
        texts = [normalize(e.text) for e in (await self.read()).events]
        self.assertIn("\U0001f602", texts)

    async def test_both_separator_formats_are_not_messages(self) -> None:
        await self.open_fixture("thread_1to1.html")
        texts = [normalize(e.text) for e in (await self.read()).events]
        for separator in ("12 Sep, 3:04", "Tue 12:03"):
            self.assertNotIn(separator, texts)

    async def test_reaction_is_never_a_message(self) -> None:
        await self.open_fixture("thread_1to1.html")
        result = await self.read()
        reactions = [e for e in result.events if e.kind == "reaction"]
        self.assertEqual(len(reactions), 1)
        self.assertNotIn("reaction", [e.kind for e in result.events
                                      if e.kind == "text"])

    async def test_expired_share_is_labelled_not_dropped(self) -> None:
        await self.open_fixture("thread_1to1.html")
        result = await self.read()
        unavailable = [e for e in result.events
                       if e.media_label == "reel_unavailable"]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0].kind, "media")

    async def test_replied_to_attaches_to_the_next_message(self) -> None:
        await self.open_fixture("thread_1to1.html")
        result = await self.read()
        with_target = [e for e in result.events if e.reply_to_text]
        self.assertEqual(len(with_target), 1)
        self.assertIn("replied to you", normalize(with_target[0].reply_to_text))

    async def test_multiline_emoji_quote_edit_system_and_unsupported(self) -> None:
        await self.open_fixture("thread_kinds.html")
        result = await self.read()
        by_text = {normalize(e.text): e for e in result.events}

        self.assertIn("line one line two", by_text)          # multiline
        self.assertIn("\U0001f602", normalize(by_text["haha \U0001f602 that was funny"].text))
        quoted = by_text["my answer to the quote"]
        self.assertEqual(quoted.reply_to_text, "quoted earlier line")
        self.assertTrue(by_text["typo fixed"].evidence.get("edited"))
        self.assertEqual(
            [e.kind for e in result.events if e.text == "This message was unsent"],
            ["system"],
        )
        # An audio card carries no media signal in the DOM, so none is invented.
        audio = by_text["Audio message"]
        self.assertIsNone(audio.media_label)
        self.assertEqual(audio.kind, "text")

    async def test_media_cards_get_a_label(self) -> None:
        await self.open_fixture("thread_media.html")
        labels = {e.media_label for e in (await self.read()).events
                  if e.kind == "media"}
        self.assertEqual(labels, {"reel", "post"})

    async def test_group_is_proved_by_profile_links_or_avatars(self) -> None:
        await self.open_fixture("thread_group.html")
        by_links = await self.read()
        self.assertIs(by_links.is_group, True)
        self.assertEqual(by_links.participant_links, 3)

        await self.open_fixture("thread_group_avatars.html")
        by_avatars = await self.read()
        self.assertIs(by_avatars.is_group, True)
        self.assertEqual(by_avatars.participant_links, 2)

        await self.open_fixture("thread_long.html")
        self.assertIs((await self.read()).is_group, False)


class B04CompletenessTest(BrowserCase):
    """B04: order preserved, repeats kept, incompleteness reported."""

    async def test_a_long_thread_keeps_order_and_both_repeats(self) -> None:
        await self.open_fixture("thread_long.html")
        result = await self.read()
        texts = [normalize(e.text) for e in result.events]
        self.assertEqual(len(texts), 45)
        self.assertEqual([e.position for e in result.events], list(range(45)))
        oks = [i for i, t in enumerate(texts) if t == "ok"]
        self.assertEqual(len(oks), 2)
        self.assertEqual(oks[1] - oks[0], 1, "the pair is adjacent in the read")

    async def test_both_separator_formats_survive_in_a_long_thread(self) -> None:
        await self.open_fixture("thread_long.html")
        texts = [normalize(e.text) for e in (await self.read()).events]
        for separator in ("12 Sep, 3:04", "Tue 12:03"):
            self.assertNotIn(separator, texts)

    async def test_a_window_that_has_not_reached_the_start_is_not_complete(self) -> None:
        await self.open_fixture("thread_long.html")
        scrolled = await self.read()
        self.assertFalse(scrolled.complete, "the fixture opens at the newest message")

        await self.page.evaluate("() => { document.querySelector('.pane').scrollTop = 0; }")
        await self.page.wait_for_timeout(60)
        at_top = await self.read()
        self.assertTrue(at_top.complete)
        self.assertEqual(len(at_top.events), len(scrolled.events))

    async def test_two_overlapping_reads_agree_on_every_position(self) -> None:
        await self.open_fixture("thread_1to1.html")
        first = await self.read()
        second = await self.read()
        self.assertEqual(
            [(e.position, e.sender, e.kind, normalize(e.text)) for e in first.events],
            [(e.position, e.sender, e.kind, normalize(e.text)) for e in second.events],
        )


class B05GuardTest(BrowserCase):
    """B05: navigation and lookalike hosts are refused."""

    def test_lookalike_hosts_http_and_odd_schemes_are_refused(self) -> None:
        from shadow.browser import OffSite, check_url, host_allowed

        for good in ("https://www.instagram.com/direct/inbox/",
                     "https://instagram.com/direct/t/12/"):
            self.assertTrue(host_allowed(good), good)
            self.assertEqual(check_url(good), good)

        for bad in ("https://evilinstagram.com/",             # suffix bypass
                    "https://instagram.com.evil.com/",        # prefix trick
                    "https://www.instagram.com.attacker.io/",
                    "http://www.instagram.com/",              # plain http
                    "https://l.instagram.com/?u=http://evil.com",
                    "https://facebook.com/",
                    "javascript:alert(1)",
                    "data:text/html,<h1>x",
                    "file:///C:/Windows/",
                    "https://127.0.0.1:9222/"):                # our own CDP host
            with self.subTest(url=bad):
                self.assertFalse(host_allowed(bad), bad)
                with self.assertRaises(OffSite):
                    check_url(bad)

    def test_a_non_loopback_cdp_endpoint_is_refused(self) -> None:
        refuse_cdp_url("http://127.0.0.1:9222")
        refuse_cdp_url("http://localhost:9222")
        for bad in ("http://10.0.0.5:9222", "http://example.com:9222",
                    "ws://192.168.1.5:9222", "http://0.0.0.0:9222"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    refuse_cdp_url(bad)

    async def test_send_one_refuses_a_file_page_even_with_a_thread_url(self) -> None:
        from shadow.instagram import InstagramBrowser

        await self.open_fixture("thread_send.html", "append_owner")
        adapter = InstagramBrowser()
        adapter.page = self.page
        result = await adapter.send_one("12345", "typed text", self._always)
        self.assertEqual(result.status, "failed_before_submit")
        self.assertEqual(result.error_code, "off_site")

    @staticmethod
    async def _always(_point: str) -> bool:
        return True


class B07ComposerTest(BrowserCase):
    """B07: a composer the owner already typed into is a hold."""

    async def test_manual_composer_text_is_never_overwritten(self) -> None:
        await self.open_fixture("thread_send.html", "append_owner")
        await self.page.evaluate(
            """() => {
                document.querySelector(
                    'div[contenteditable="true"][aria-label="Message"]'
                ).innerText = 'the owner already typed this';
            }"""
        )
        typed = []

        async def check(point: str) -> bool:
            typed.append(point)
            return True

        result = await _send_on_page(self.page, "12345", "shadow text", check)
        self.assertEqual(result.status, "failed_before_submit")
        self.assertEqual(result.error_code, "manual_text")
        self.assertEqual(typed, [], "nothing may be typed after a manual hold")
        self.assertEqual(await self._composer_text(), "the owner already typed this")

    async def test_abort_before_enter_clears_the_composer(self) -> None:
        await self.open_fixture("thread_send.html", "append_owner")
        seen = []

        async def check(point: str) -> bool:
            seen.append(point)
            return point != "before_enter"

        result = await _send_on_page(self.page, "12345", "shadow text", check)
        self.assertEqual(result.status, "failed_before_submit")
        self.assertEqual(result.error_code, "aborted_before_enter")
        self.assertEqual(seen, ["before_type", "before_enter"])
        self.assertEqual(await self._composer_text(), "")

    async def test_abort_before_type_types_nothing(self) -> None:
        await self.open_fixture("thread_send.html", "append_owner")

        async def check(point: str) -> bool:
            return point != "before_type"

        result = await _send_on_page(self.page, "12345", "shadow text", check)
        self.assertEqual(result.status, "failed_before_submit")
        self.assertEqual(result.error_code, "aborted_before_type")
        self.assertEqual(await self._composer_text(), "")

    async def _composer_text(self) -> str:
        return (await self.page.evaluate(
            """() => {
                const b = document.querySelector('div[contenteditable="true"]'
                                                 + '[aria-label="Message"]');
                return b ? (b.innerText || '').trim() : '';
            }"""
        )) or ""


class B08PreviewTest(BrowserCase):
    """B08: preview-only classes are read from the row, nothing is opened."""

    async def test_preview_classes_come_from_the_row_alone(self) -> None:
        await self.open_fixture("inbox.html")
        result = await self.inbox()
        self.assertEqual(result.status, "ok")
        by_label = {r.label: r for r in result.rows}
        self.assertEqual(by_label["Test Contact Three"].preview_class, "text_in")
        self.assertTrue(by_label["Test Contact Three"].unread_hint)
        self.assertEqual(by_label["Test Owner"].preview_class, "owner_last")
        self.assertFalse(by_label["Test Owner"].unread_hint)
        self.assertEqual(by_label["Test Contact One"].preview_class, "attachment_in")
        for row in result.rows:
            self.assertNotIn("·", row.preview_text)
            self.assertNotRegex(row.preview_text, r"\d+[smhdw]$")
            self.assertIsNone(row.group_hint)

    async def test_reading_the_inbox_never_opens_a_thread(self) -> None:
        await self.open_fixture("inbox.html")
        before = self.page.url
        result = await self.inbox()
        self.assertEqual(self.page.url, before, "an inbox read must not navigate")
        self.assertEqual(result.navigations_used, 0)


class B09PauseTest(BrowserCase):
    """B09: receipts, challenge and login are pause states, not reads."""

    async def test_a_visible_receipt_is_receipts_visible(self) -> None:
        await self.open_fixture("seen.html")
        result = await self.read()
        self.assertEqual(result.status, "receipts_visible")

    async def test_a_challenge_path_is_a_challenge(self) -> None:
        await self.open_fixture("screen_checkpoint.html")
        await self.set_path("/challenge/entry/")
        result = await self.read()
        self.assertEqual(result.status, "challenge")
        self.assertEqual(result.events, ())

    async def test_a_suspended_path_is_a_challenge(self) -> None:
        await self.open_fixture("screen_checkpoint.html")
        await self.set_path("/accounts/suspended/")
        self.assertEqual((await self.read()).status, "challenge")

    async def test_a_login_path_is_login_required(self) -> None:
        await self.open_fixture("screen_login.html")
        await self.set_path("/accounts/login/")
        self.assertEqual((await self.read()).status, "login_required")
        self.assertEqual((await self.inbox()).status, "login_required")

    async def test_pause_statuses_are_the_global_pause_set(self) -> None:
        from shadow.contracts import GLOBAL_PAUSE_STATUSES

        self.assertIn("receipts_visible", GLOBAL_PAUSE_STATUSES)
        self.assertIn("challenge", GLOBAL_PAUSE_STATUSES)
        self.assertIn("login_required", GLOBAL_PAUSE_STATUSES)


class SendEvidenceTest(BrowserCase):
    """D01-D03 and D07: the send primitive against the composer fixture."""

    async def _send(self, mode: str, text: str = "pong",
                    check=None) -> tuple:
        await self.open_fixture("thread_send.html", mode)
        calls: list[str] = []
        if check is None:
            async def check(point: str) -> bool:
                calls.append(point)
                return True
        return await _send_on_page(self.page, "12345", text, check,
                                   timeout=CONFIRM_TIMEOUT), calls

    async def test_d03_a_new_owner_bubble_is_confirmed_exactly_once(self) -> None:
        result, calls = await self._send("append_owner")
        self.assertEqual(result.status, "confirmed")
        self.assertIsNotNone(result.matched_position)
        self.assertEqual(result.evidence["matches_before"], 2)
        self.assertEqual(result.evidence["matches_after"], 3)
        self.assertEqual(calls, ["before_type", "before_enter"])

        final = await self.read()
        owner_oks = [e for e in final.events
                     if e.sender == "owner" and e.text == "pong"]
        self.assertEqual(len(owner_oks), 3, "exactly one bubble was added")
        self.assertEqual(owner_oks[-1].position, result.matched_position)

    async def test_d03_a_delayed_bubble_is_still_confirmed(self) -> None:
        result, _ = await self._send("append_owner_delayed")
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.evidence["matches_before"], 2)

    async def test_d01_an_incoming_append_is_never_confirmation(self) -> None:
        result, _ = await self._send("append_incoming", "shadow text")
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.error_code, "no_confirmation")
        self.assertEqual(result.evidence["matches_before"], 0)
        self.assertEqual(result.evidence["matches_after"], 0)
        self.assertGreater(result.evidence["events"], 0,
                           "the read still saw the inbound line")

    async def test_d02_an_old_identical_bubble_does_not_confirm(self) -> None:
        result, _ = await self._send("duplicate_old", "pong")
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.error_code, "no_confirmation")
        self.assertEqual(result.evidence["matches_before"], 2)
        self.assertEqual(result.evidence["matches_after"], 2)

    async def test_d07_nothing_happening_is_uncertain_not_failed(self) -> None:
        result, _ = await self._send("nothing")
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.error_code, "no_confirmation")

    async def test_d07_an_error_marker_beats_the_bubble(self) -> None:
        result, _ = await self._send("error_marker", "shadow text")
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.error_code, "error_marker")
        self.assertEqual(result.evidence["matches_after"], 1,
                         "the bubble was there; the error marker still wins")

    async def test_a_count_based_confirmation_survives_an_old_identical_bubble(
        self,
    ) -> None:
        """Two 'pong' already on screen, then a third appears: confirmed."""
        await self.open_fixture("thread_send.html", "append_owner")
        final = await self.read()
        self.assertEqual(
            len([e for e in final.events if e.sender == "owner" and e.text == "pong"]),
            2,
        )
        result, _ = await self._send("append_owner", "pong")   # fresh page
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.evidence["matches_before"], 2)
        self.assertEqual(result.evidence["matches_after"], 3)

    async def test_nothing_is_typed_when_the_pane_cannot_be_read(self) -> None:
        await self.open_fixture("thread_no_pane.html")
        calls: list[str] = []

        async def check(point: str) -> bool:
            calls.append(point)
            return True

        result = await _send_on_page(self.page, "12345", "text", check,
                                     timeout=CONFIRM_TIMEOUT)
        self.assertEqual(result.status, "failed_before_submit")
        self.assertEqual(result.error_code, "read_dom_unknown")
        self.assertEqual(calls, [])


class NoLegacyTest(unittest.TestCase):
    """The new modules must not reintroduce what P00b found broken.

    Behavioural equivalents of the legacy source-text checks, scoped to the two
    new files only. shadow/browser.py keeps its own copy until the integration
    step removes it.
    """

    def setUp(self) -> None:
        self.dom = (Path(__file__).resolve().parents[2]
                    / "shadow" / "dom.py").read_text(encoding="utf-8")
        self.adapter = (Path(__file__).resolve().parents[2]
                        / "shadow" / "instagram.py").read_text(encoding="utf-8")

    def test_no_sender_memo_and_no_persistent_context(self) -> None:
        for name, src in (("dom.py", self.dom), ("instagram.py", self.adapter)):
            with self.subTest(module=name):
                self.assertNotIn("__shSender", src)
                self.assertNotIn("launch_persistent_context", src)
                self.assertNotIn("screenshot", src.lower())

    def test_the_host_guard_is_used_before_typing(self) -> None:
        self.assertIn("host_allowed(url)", self.adapter)
        self.assertIn("check_url(url)", self.adapter)
        self.assertIn("_is_main_frame", self.adapter)

    def test_no_legacy_parsers_are_imported(self) -> None:
        for legacy in ("_JS_INBOX", "_JS_READ", "_JS_HAS_PANE", "NOISE"):
            with self.subTest(symbol=legacy):
                self.assertNotIn(legacy, self.dom)
                self.assertNotIn(legacy, self.adapter)

    def test_the_adapter_never_reimplements_the_reader(self) -> None:
        """The reader JS lives in shadow/dom.py and nowhere else."""
        from shadow import dom

        self.assertIn("role=\"article\"", self.dom)
        self.assertIn("role=\"article\"", dom.JS_RAW)
        self.assertNotIn("role=\"article\"", self.adapter)
        self.assertNotIn("querySelectorAll('[dir=\"auto\"]')", self.adapter)


if __name__ == "__main__":
    unittest.main()

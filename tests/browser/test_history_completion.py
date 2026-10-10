"""Production-reader regressions for bounded, virtualized history completion."""

from __future__ import annotations

from unittest.mock import patch

from test_reader import BrowserCase


class HistoryCompletionTest(BrowserCase):
    async def test_requested_count_can_exceed_static_step_count_with_verified_progress(self):
        """Synthetic one-message windows must not stop at the 24-step floor."""
        from shadow.instagram import InstagramBrowser
        from shadow.settings import Settings

        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                if self.page:
                    await self.page.close()
                    self.page = None
                await self.open_fixture('thread_long.html')
                await self.page.evaluate("""reverse => {
                    const pane = document.querySelector('.pane');
                    if (reverse) pane.style.flexDirection = 'column-reverse';
                    pane.innerHTML = Array.from({length: 29}, (_, i) =>
                        '<div role="article"><span dir="auto">new-' + i + '</span></div>').join('');
                    let older = 0;
                    pane.scrollBy = () => {
                        pane.setAttribute('aria-busy', 'true');
                        setTimeout(() => {
                            pane.insertAdjacentHTML('afterbegin',
                                '<div role="article"><span dir="auto">old-' + older++ + '</span></div>');
                            pane.removeAttribute('aria-busy');
                        }, 15);
                    };
                }""", reverse)
                adapter = InstagramBrowser(settings=Settings(
                    dom_read_timeout_ms=5_000, dom_settle_ms=10, dom_sample_ms=5,
                    history_step_timeout_ms=250,
                ))
                adapter.page = self.page
                with patch.object(adapter, '_assert_on_site'):
                    result = await adapter._collect_history(60)

                self.assertEqual(result.status, 'ok', result.detail)
                self.assertEqual(result.detail, 'history_count_reached')
                self.assertEqual(sum(e.kind in ('text', 'media', 'unknown') for e in result.events), 60)
                self.assertFalse(result.complete)

    async def test_actual_scroll_waits_for_a_delayed_window_replacement(self):
        """A real pane scroll settles only after its delayed replacement window renders."""
        from shadow.dom import JS_RAW
        from shadow.instagram import _JS_WAIT_RAW

        await self.open_fixture('thread_long.html')
        await self.set_path('/direct/t/12345/')
        await self.page.evaluate("""() => {
            const pane = document.querySelector('.pane');
            pane.style.cssText = 'height:400px;flex:none;min-height:0;overflow-y:auto;';
            const window = texts => '<div style="height:1600px"></div>' + texts.map(text =>
                '<div role="article"><span dir="auto">' + text + '</span></div>').join('');
            pane.innerHTML = window(['a', 'b', 'c']); pane.scrollTop = pane.scrollHeight;
            pane.addEventListener('scroll', () => {
                pane.setAttribute('aria-busy', 'true');
                setTimeout(() => {
                    pane.innerHTML = window(['older', 'a', 'b']);
                    pane.removeAttribute('aria-busy');
                }, 20);
            }, {once: true});
        }""")
        settled = await self.page.evaluate(_JS_WAIT_RAW,
            [JS_RAW, True, '/direct/t/12345/', 250, 10, 5, True, 80])

        self.assertEqual(settled['reason'], 'settled')
        self.assertEqual([a['text'] for a in settled['raw']['articles']], ['older', 'a', 'b'])

    async def test_extended_request_still_holds_when_loading_does_not_settle(self):
        from shadow.instagram import InstagramBrowser
        from shadow.settings import Settings

        await self.open_fixture('thread_long.html')
        await self.page.evaluate("""() => {
            const pane = document.querySelector('.pane');
            pane.style.cssText = 'height:400px;flex:none;min-height:0;overflow-y:auto;';
            pane.innerHTML = '<div style="height:1600px"></div>' + Array.from({length: 29}, (_, i) =>
                '<div role="article"><span dir="auto">message-' + i + '</span></div>').join('');
            pane.scrollTop = pane.scrollHeight;
        }""")
        adapter = InstagramBrowser(settings=Settings(
            dom_read_timeout_ms=1_000, dom_settle_ms=10, dom_sample_ms=5,
            history_step_timeout_ms=250,
        ))
        adapter.page = self.page
        with patch.object(adapter, '_assert_on_site'):
            result = await adapter._load_history(60)

        self.assertEqual(result.status, 'dom_unknown')
        self.assertEqual(result.detail, 'history_load_unsettled')
        self.assertFalse(result.complete)

    async def test_receipt_pause_preserves_already_verified_history_window(self):
        from shadow.alignment import sequence_events
        from shadow.instagram import InstagramBrowser

        await self.open_fixture('thread_long.html')
        await self.set_path('/direct/t/12345/')
        expected = tuple(sequence_events((await self.read()).events))
        await self.page.evaluate("""() => {
            const pane = document.querySelector('.pane');
            pane.addEventListener('scroll', () => {
                pane.insertAdjacentHTML('beforeend', '<span dir="auto">Seen</span>');
            }, {once: true});
        }""")
        adapter = InstagramBrowser(); adapter.page = self.page
        with patch.object(adapter, '_assert_on_site'):
            result = await adapter._collect_history(100)

        self.assertEqual(result.status, 'receipts_visible')
        self.assertEqual(result.events, expected)
        self.assertFalse(result.complete)

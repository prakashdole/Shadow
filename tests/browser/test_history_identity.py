"""Browser-level runtime-identity history-merge regressions."""

from __future__ import annotations

from unittest.mock import patch

from test_reader import BrowserCase


class HistoryIdentityTest(BrowserCase):
    async def collect(self, current, older, *, want=3, guarded=False):
        from shadow.instagram import InstagramBrowser

        await self.open_fixture('thread_long.html')
        await self.set_path('/direct/t/12345/')
        await self.page.evaluate("""([current, older]) => {
            const pane = document.querySelector('.pane');
            const render = rows => {
                pane.innerHTML = rows.map(({text, id}) =>
                    '<div role="article"><span dir="auto">' + text + '</span></div>').join('');
                for (const [index, article] of [...pane.querySelectorAll('[role="article"]')].entries()) {
                    const {id, conflict} = rows[index];
                    if (!id) continue;
                    const root = {tag: 3, flags: 0}; root.stateNode = {current: root};
                    const current = {return: root, flags: 0, memoizedProps: {currentMessageRef: {id}}};
                    root.child = current;
                    const direct = {return: current, flags: 0, memoizedProps: {messageId: id}};
                    current.child = direct;
                    if (conflict) direct.memoizedProps.messageId = id + '-conflict';
                    article.__reactFiber$fixture = direct;
                }
            };
            render(current);
            pane.scrollBy = () => render(older);
        }""", [current, older])
        adapter = InstagramBrowser(); adapter.page = self.page
        with patch.object(adapter, '_assert_on_site'):
            return await (adapter._load_history(want) if guarded
                          else adapter._collect_history(want))

    async def test_runtime_identity_merges_repeated_text_virtual_window(self):
        result = await self.collect(
            [{'text': 'same', 'id': 'b'}, {'text': 'same', 'id': 'c'}],
            [{'text': 'same', 'id': 'a'}, {'text': 'same', 'id': 'b'}],
        )

        self.assertEqual(result.status, 'ok', result.detail)
        self.assertEqual(result.detail, 'history_count_reached')
        self.assertEqual([event.evidence['runtime_message_id'] for event in result.events],
                         ['a', 'b', 'c'])
        self.assertFalse(result.complete)

    async def test_missing_identity_falls_back_to_content_but_conflicts_hold(self):
        with self.subTest('missing_identity'):
            result = await self.collect(
                [{'text': 'b', 'id': None}, {'text': 'c', 'id': None}],
                [{'text': 'a', 'id': None}, {'text': 'b', 'id': None}],
            )
            self.assertEqual(result.status, 'ok', result.detail)
            self.assertEqual([event.text for event in result.events], ['a', 'b', 'c'])

        with self.subTest('duplicate_identity'):
            result = await self.collect(
                [{'text': 'same', 'id': 'b'}, {'text': 'same', 'id': 'c'}],
                [{'text': 'same', 'id': 'b'}, {'text': 'same', 'id': 'b'}],
            )
            self.assertEqual(result.detail, 'history_ambiguous')
            self.assertFalse(result.complete)

        with self.subTest('identity_content_conflict'):
            result = await self.collect(
                [{'text': 'before', 'id': 'b'}, {'text': 'c', 'id': 'c'}],
                [{'text': 'a', 'id': 'a'}, {'text': 'changed', 'id': 'b'}],
            )
            self.assertEqual(result.detail, 'history_gap')
            self.assertFalse(result.complete)

    async def test_runtime_conflicts_and_duplicates_hold_before_count_or_merge(self):
        with self.subTest('initial_conflict'):
            result = await self.collect(
                [{'text': 'a', 'id': 'a', 'conflict': True}, {'text': 'b', 'id': 'b'}], [],
                want=2, guarded=True,
            )
            self.assertEqual(result.detail, 'history_gap')
            self.assertFalse(result.complete)

        with self.subTest('older_conflict'):
            result = await self.collect(
                [{'text': 'b', 'id': 'b'}, {'text': 'c', 'id': 'c'}],
                [{'text': 'a', 'id': 'a', 'conflict': True}, {'text': 'b', 'id': 'b'}],
            )
            self.assertEqual(result.detail, 'history_gap')
            self.assertFalse(result.complete)

        with self.subTest('initial_duplicate'):
            result = await self.collect(
                [{'text': 'same', 'id': 'a'}, {'text': 'same', 'id': 'a'}], [],
                want=2, guarded=True,
            )
            self.assertEqual(result.detail, 'history_ambiguous')
            self.assertFalse(result.complete)

    async def test_restoration_requires_compatible_known_runtime_ids(self):
        from shadow.instagram import InstagramBrowser

        async def load(restored, *, current=None, older=None, want=3):
            await self.open_fixture('thread_long.html')
            await self.set_path('/direct/t/12345/')
            current = current or [{'text': 'b', 'id': 'b'}, {'text': 'c', 'id': 'c'}]
            older = older or [{'text': 'a', 'id': 'a'}, {'text': 'b', 'id': 'b'}]
            await self.page.evaluate("""([current, older]) => {
                const pane = document.querySelector('.pane');
                window.__renderIdentityRows = rows => {
                    pane.innerHTML = rows.map(({text}) =>
                        '<div role="article"><span dir="auto">' + text + '</span></div>').join('');
                    for (const [index, article] of [...pane.querySelectorAll('[role="article"]')].entries()) {
                        const id = rows[index].id;
                        if (!id) continue;
                        const root = {tag: 3, flags: 0}; root.stateNode = {current: root};
                        const current = {return: root, flags: 0, memoizedProps: {currentMessageRef: {id}}};
                        root.child = current;
                        const direct = {return: current, flags: 0, memoizedProps: {messageId: id}};
                        current.child = direct;
                        article.__reactFiber$fixture = direct;
                    }
                };
                pane.scrollBy = () => window.__renderIdentityRows(older);
                window.__renderIdentityRows(current);
            }""", [current, older])
            adapter = InstagramBrowser(); adapter.page = self.page
            collect = adapter._collect_history

            async def after_collect(*args, **kwargs):
                result = await collect(*args, **kwargs)
                await self.page.evaluate('rows => window.__renderIdentityRows(rows)', restored)
                return result

            with patch.object(adapter, '_assert_on_site'), \
                    patch.object(adapter, '_collect_history', side_effect=after_collect):
                return await adapter._load_history(want)

        with self.subTest('changed_known_id'):
            result = await load([{'text': 'b', 'id': 'x'}, {'text': 'c', 'id': 'y'}])
            self.assertEqual(result.status, 'dom_unknown')
            self.assertEqual(result.detail, 'history_view_restore_failed')
            self.assertFalse(result.coverage['view_content_restored'])

        if self.page:
            await self.page.close()
            self.page = None
        with self.subTest('missing_id_fallback'):
            result = await load([{'text': 'b', 'id': None}, {'text': 'c', 'id': None}])
            self.assertEqual(result.status, 'ok', result.detail)
            self.assertTrue(result.coverage['view_content_restored'])

        if self.page:
            await self.page.close()
            self.page = None
        with self.subTest('missing_id_cannot_mask_changed_shared_id'):
            result = await load(
                [{'text': 'same', 'id': None}, {'text': 'changed', 'id': 'stable'}],
                current=[{'text': 'same', 'id': 'stable'}],
                older=[{'text': 'older', 'id': 'older'}, {'text': 'same', 'id': 'stable'}],
                want=2,
            )
            self.assertEqual(result.status, 'dom_unknown')
            self.assertEqual(result.detail, 'history_view_restore_failed')
            self.assertFalse(result.coverage['view_content_restored'])

        if self.page:
            await self.page.close()
            self.page = None
        with self.subTest('partial_suffix_is_not_restoration'):
            result = await load(
                [{'text': 'b', 'id': None}],
                current=[{'text': 'a', 'id': 'a'}, {'text': 'b', 'id': 'b'}],
                older=[{'text': 'older', 'id': 'older'}, {'text': 'a', 'id': 'a'},
                       {'text': 'b', 'id': 'b'}],
                want=3,
            )
            self.assertEqual(result.status, 'dom_unknown')
            self.assertEqual(result.detail, 'history_view_restore_failed')
            self.assertFalse(result.coverage['view_content_restored'])

    async def test_receipt_pause_precedes_manual_text_without_history_scroll(self):
        from shadow.instagram import InstagramBrowser

        await self.open_fixture('seen.html')
        await self.set_path('/direct/t/12345/')
        await self.page.evaluate("""() => {
            document.querySelector('[contenteditable="true"]').textContent = 'owner draft';
            window.__historyScrolls = 0;
            document.querySelector('.pane').scrollBy = () => { window.__historyScrolls += 1; };
        }""")
        adapter = InstagramBrowser(); adapter.page = self.page
        with patch.object(adapter, '_assert_on_site'):
            result = await adapter._load_history(10)

        self.assertEqual(result.status, 'receipts_visible')
        self.assertEqual(await self.page.evaluate('window.__historyScrolls'), 0)

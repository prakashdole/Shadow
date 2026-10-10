"""Fictional runtime-fiber shapes for optional message metadata extraction."""

from __future__ import annotations

import unittest
from pathlib import Path

from shadow.dom import JS_RAW, interpret_read
from shadow.dom_identity import RUNTIME_MESSAGE_METADATA_JS


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "dom"


class RuntimeIdentityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch()
        except Exception as exc:
            self.skipTest(f"headless Chromium unavailable: {type(exc).__name__}: {str(exc)[:160]}")
        self.page = await self._browser.new_page()
        await self.page.set_content('<div role="article"></div>')

    async def asyncTearDown(self) -> None:
        await self.page.close()
        await self._browser.close()
        await self._playwright.stop()

    async def metadata(self, shape: str):
        await self.page.evaluate(f"() => {{ {shape} }}")
        return await self.page.locator('[role="article"]').evaluate(RUNTIME_MESSAGE_METADATA_JS)

    async def test_current_fiber_returns_only_correlated_direct_metadata(self) -> None:
        result = await self.metadata("""
            const article = document.querySelector('[role=article]');
            const pair = (a, b) => { a.alternate = b; b.alternate = a; };
            const root = {tag: 3}, oldRoot = {tag: 3}; pair(root, oldRoot); root.stateNode = {current: root}; oldRoot.stateNode = root.stateNode;
            const host = {return: root}, oldHost = {return: oldRoot}; pair(host, oldHost); root.child = host; oldRoot.child = oldHost;
            const direction = {return: host, memoizedProps: {outgoing: false}}, oldDirection = {return: oldHost}; pair(direction, oldDirection); host.child = direction; oldHost.child = oldDirection;
            const slide = {return: direction, memoizedProps: {slideMessageRef: {timestamp_ms: '1700000000000', sender_fbid: '42'}}}, oldSlide = {return: oldDirection}; pair(slide, oldSlide); direction.child = slide; oldDirection.child = oldSlide;
            const current = {return: slide, memoizedProps: {currentMessageRef: {id: 'message-1', timestamp_ms: '1700000000000', sender_fbid: '42'}}}, oldCurrent = {return: oldSlide}; pair(current, oldCurrent); slide.child = current; oldSlide.child = oldCurrent;
            const direct = {return: current, memoizedProps: {messageId: 'message-1'}}, oldDirect = {return: oldCurrent}; pair(direct, oldDirect); current.child = direct; oldCurrent.child = oldDirect;
            article.__reactFiber$fiction = direct;
        """)
        self.assertEqual(result, {"runtime_message_id": "message-1", "runtime_outgoing": False,
                                  "runtime_timestamp_ms": "1700000000000", "runtime_sender_fbid": "42"})

    async def test_mounted_fiber_without_an_alternate_requires_no_pending_insertion(self) -> None:
        result = await self.metadata("""
            const article = document.querySelector('[role=article]');
            const root = {tag: 3, flags: 0}; root.stateNode = {current: root};
            const current = {return: root, flags: 0, memoizedProps: {currentMessageRef: {id: 'message-1'}}}; root.child = current;
            const direct = {return: current, flags: 0, memoizedProps: {messageId: 'message-1'}}; current.child = direct;
            article.__reactFiber$fiction = direct;
        """)
        self.assertEqual(result, {"runtime_message_id": "message-1"})
        await self.page.set_content('<div role="article"></div>')
        result = await self.metadata("""
            const article = document.querySelector('[role=article]');
            const root = {tag: 3, flags: 0}; root.stateNode = {current: root};
            const current = {return: root, flags: 0, memoizedProps: {currentMessageRef: {id: 'message-1'}}}; root.child = current;
            const direct = {return: current, flags: 2, memoizedProps: {messageId: 'message-1'}}; current.child = direct;
            article.__reactFiber$fiction = direct;
        """)
        self.assertIsNone(result)
        await self.page.set_content('<div role="article"></div>')
        self.assertIsNone(await self.metadata("""
            const article = document.querySelector('[role=article]');
            const root = {tag: 3, flags: 0}; root.stateNode = {current: root};
            root.child = {return: root, flags: 0};
            const detached = {return: root, flags: 0, memoizedProps: {
                messageId: 'message-1', currentMessageRef: {id: 'message-1'}}};
            article.__reactFiber$fiction = detached;
        """))

    async def test_stale_attached_fiber_uses_its_unique_current_alternate(self) -> None:
        result = await self.metadata("""
            const article = document.querySelector('[role=article]');
            const pair = (a, b) => { a.alternate = b; b.alternate = a; };
            const root = {tag: 3}, oldRoot = {tag: 3}; pair(root, oldRoot); root.stateNode = {current: root}; oldRoot.stateNode = root.stateNode;
            const host = {return: root}, oldHost = {return: oldRoot}; pair(host, oldHost); root.child = host; oldRoot.child = oldHost;
            const current = {return: host, memoizedProps: {currentMessageRef: {id: 'fresh'}},}, oldCurrent = {return: oldHost, memoizedProps: {currentMessageRef: {id: 'stale'}}}; pair(current, oldCurrent); host.child = current; oldHost.child = oldCurrent;
            const direct = {return: current, memoizedProps: {messageId: 'fresh'}}, oldDirect = {return: oldCurrent, memoizedProps: {messageId: 'stale'}}; pair(direct, oldDirect); current.child = direct; oldCurrent.child = oldDirect;
            article.__reactFiber$fiction = oldDirect;
        """)
        self.assertEqual(result, {"runtime_message_id": "fresh"})

    async def test_detached_current_alternate_is_omitted(self) -> None:
        result = await self.metadata("""
            const article = document.querySelector('[role=article]');
            const pair = (a, b) => { a.alternate = b; b.alternate = a; };
            const root = {tag: 3}, oldRoot = {tag: 3}; pair(root, oldRoot); root.stateNode = {current: root}; oldRoot.stateNode = root.stateNode;
            const host = {return: root}, oldHost = {return: oldRoot}; pair(host, oldHost); root.child = {return: root}; oldRoot.child = oldHost;
            const current = {return: host, memoizedProps: {currentMessageRef: {id: 'fresh'}}}, oldCurrent = {return: oldHost}; pair(current, oldCurrent); host.child = current; oldHost.child = oldCurrent;
            const direct = {return: current, memoizedProps: {messageId: 'fresh'}}, oldDirect = {return: oldCurrent}; pair(direct, oldDirect); current.child = direct; oldCurrent.child = oldDirect;
            article.__reactFiber$fiction = oldDirect;
        """)
        self.assertIsNone(result)

    async def test_conflicting_or_partial_current_metadata_is_omitted(self) -> None:
        for shape in ("""
            const article = document.querySelector('[role=article]');
            const pair = (a, b) => { a.alternate = b; b.alternate = a; };
            const root = {tag: 3}, oldRoot = {tag: 3}; pair(root, oldRoot); root.stateNode = {current: root}; oldRoot.stateNode = root.stateNode;
            const host = {return: root}, oldHost = {return: oldRoot}; pair(host, oldHost); root.child = host; oldRoot.child = oldHost;
            const current = {return: host, memoizedProps: {currentMessageRef: {id: 'one'}}}, oldCurrent = {return: oldHost}; pair(current, oldCurrent); host.child = current; oldHost.child = oldCurrent;
            const direct = {return: current, memoizedProps: {messageId: 'two'}}, oldDirect = {return: oldCurrent}; pair(direct, oldDirect); current.child = direct; oldCurrent.child = oldDirect;
            article.__reactFiber$fiction = direct;
        """,):
            await self.page.set_content('<div role="article"></div>')
            await self.page.evaluate(f"() => {{ {shape} }}")
            self.assertEqual(
                await self.page.locator('[role="article"]').evaluate(RUNTIME_MESSAGE_METADATA_JS),
                {"runtime_identity_conflict": True},
            )
        await self.page.set_content('<div role="article"></div>')
        self.assertIsNone(await self.metadata("""
            const article = document.querySelector('[role=article]');
            const root = {tag: 3, flags: 0}; root.stateNode = {current: root};
            const current = {return: root, flags: 0, memoizedProps: {currentMessageRef: {}}}; root.child = current;
            const direct = {return: current, flags: 0, memoizedProps: {messageId: 'one'}}; current.child = direct;
            article.__reactFiber$fiction = direct;
        """))

    async def test_conflicting_optional_fields_do_not_override_direct_identity(self) -> None:
        result = await self.metadata("""
            const article = document.querySelector('[role=article]');
            const pair = (a, b) => { a.alternate = b; b.alternate = a; };
            const root = {tag: 3}, oldRoot = {tag: 3}; pair(root, oldRoot); root.stateNode = {current: root}; oldRoot.stateNode = root.stateNode;
            const host = {return: root}, oldHost = {return: oldRoot}; pair(host, oldHost); root.child = host; oldRoot.child = oldHost;
            const direction = {return: host, memoizedProps: {outgoing: true}}, oldDirection = {return: oldHost}; pair(direction, oldDirection); host.child = direction; oldHost.child = oldDirection;
            const slide = {return: direction, memoizedProps: {slideMessageRef: {timestamp_ms: '1700000000000', sender_fbid: '42'}}}, oldSlide = {return: oldDirection}; pair(slide, oldSlide); direction.child = slide; oldDirection.child = oldSlide;
            const current = {return: slide, memoizedProps: {outgoing: false, currentMessageRef: {id: 'message-1', timestamp_ms: '1700000000001', sender_fbid: '43'}}}, oldCurrent = {return: oldSlide}; pair(current, oldCurrent); slide.child = current; oldSlide.child = oldCurrent;
            const direct = {return: current, memoizedProps: {messageId: 'message-1'}}, oldDirect = {return: oldCurrent}; pair(direct, oldDirect); current.child = direct; oldCurrent.child = oldDirect;
            article.__reactFiber$fiction = direct;
        """)
        self.assertEqual(result, {"runtime_message_id": "message-1", "runtime_outgoing_conflict": True})


class RuntimeIdentityReaderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch()
        except Exception as exc:
            self.skipTest(f"headless Chromium unavailable: {type(exc).__name__}: {str(exc)[:160]}")
        self.page = await self._browser.new_page()

    async def asyncTearDown(self) -> None:
        await self.page.close()
        await self._browser.close()
        await self._playwright.stop()

    async def open_fixture(self) -> None:
        await self.page.goto((FIXTURES / "thread_1to1.html").as_uri(), wait_until="load")

    async def attach_runtime(self, mode: str) -> None:
        await self.page.evaluate("""mode => {
            for (const [index, article] of [...document.querySelectorAll('[role="article"]')].entries()) {
                const id = 'fixture-runtime-' + index;
                const root = {tag: 3, flags: 0}; root.stateNode = {current: root};
                const direction = {return: root, flags: 0, memoizedProps: {outgoing: index % 2 === 0}}; root.child = direction;
                const current = {return: direction, flags: 0, memoizedProps: {currentMessageRef: {id}}}; direction.child = current;
                const direct = {return: current, flags: 0, memoizedProps: {messageId: id}}; current.child = direct;
                if (mode === 'identity' && index === 0) direct.memoizedProps.messageId = id + '-conflict';
                if (mode === 'outgoing' && index === 1) current.memoizedProps.outgoing = !(index % 2 === 0);
                article.__reactFiber$fiction = direct;
            }
        }""", mode)

    async def test_absent_runtime_metadata_leaves_raw_article_keys_unchanged(self) -> None:
        await self.open_fixture()
        first = await self.page.evaluate(JS_RAW)
        second = await self.page.evaluate(JS_RAW)
        self.assertEqual(first["articles"], second["articles"])
        self.assertTrue(all(not any(key.startswith("runtime_") for key in article)
                            for article in first["articles"]))

    async def test_runtime_metadata_is_optional_raw_and_event_evidence(self) -> None:
        await self.open_fixture()
        await self.attach_runtime('success')
        raw = await self.page.evaluate(JS_RAW)
        self.assertTrue(all(article["runtime_message_id"].startswith("fixture-runtime-")
                            for article in raw["articles"]))
        self.assertTrue(all(article["runtime_identity_source"] == "react_current_message_v1"
                            for article in raw["articles"]))
        result = interpret_read(raw, "12345")
        observed = [event for event in result.events if "runtime_message_id" in event.evidence]
        self.assertEqual(len(observed), len(raw["articles"]))
        self.assertTrue(all(isinstance(event.evidence["runtime_outgoing"], bool)
                            for event in observed))
        self.assertTrue(all(event.evidence["runtime_identity_source"] == "react_current_message_v1"
                            for event in observed))

    async def test_runtime_conflicts_hold_sender_at_unknown(self) -> None:
        await self.open_fixture()
        await self.attach_runtime('identity')
        await self.attach_runtime('outgoing')
        raw = await self.page.evaluate(JS_RAW)
        # Reapply the identity conflict after the second fixture setup.
        await self.page.evaluate("""() => {
            const article = document.querySelector('[role="article"]');
            const fiber = Object.values(Object.getOwnPropertyDescriptors(article))
                .map(descriptor => descriptor.value).find(value => value && value.memoizedProps && value.memoizedProps.messageId);
            fiber.memoizedProps.messageId += '-conflict';
        }""")
        raw = await self.page.evaluate(JS_RAW)
        result = interpret_read(raw, "12345")
        conflicted = [event for event in result.events
                      if event.evidence.get("runtime_identity_conflict")
                      or event.evidence.get("runtime_outgoing_conflict")]
        self.assertEqual(len(conflicted), 2)
        self.assertTrue(all(event.sender == "unknown"
                            and event.evidence["sender_basis"] == "runtime_conflicting"
                            for event in conflicted))

    async def test_runtime_outgoing_only_checks_known_dom_sender(self) -> None:
        await self.open_fixture()
        baseline = interpret_read(await self.page.evaluate(JS_RAW), "12345")
        self.assertIn(baseline.events[0].sender, ("owner", "contact"))
        await self.attach_runtime('success')
        await self.page.evaluate("""outgoing => {
            const article = document.querySelector('[role="article"]');
            const direct = Object.values(Object.getOwnPropertyDescriptors(article))
                .map(descriptor => descriptor.value).find(value => value && value.memoizedProps?.messageId);
            direct.return.return.memoizedProps.outgoing = outgoing;
        }""", baseline.events[0].sender != "owner")
        result = interpret_read(await self.page.evaluate(JS_RAW), "12345")
        self.assertEqual(result.events[0].sender, "unknown")
        self.assertTrue(result.events[0].evidence["runtime_outgoing_sender_conflict"])

    async def test_runtime_outgoing_never_attributes_an_unknown_dom_sender(self) -> None:
        await self.open_fixture()
        await self.page.evaluate("""() => {
            document.querySelector('.pane').style.direction = 'rtl';
            for (const article of document.querySelectorAll('[role="article"]')) {
                article.removeAttribute('aria-labelledby');
                article.removeAttribute('aria-label');
            }
        }""")
        await self.attach_runtime('success')
        result = interpret_read(await self.page.evaluate(JS_RAW), "12345")
        first = result.events[0]
        self.assertEqual(first.sender, "unknown")
        self.assertEqual(first.evidence["sender_basis"], "unavailable")
        self.assertIsInstance(first.evidence["runtime_outgoing"], bool)
        self.assertNotIn("runtime_outgoing_sender_conflict", first.evidence)

    async def test_optional_reflection_error_does_not_erase_outgoing_conflict(self) -> None:
        await self.open_fixture()
        await self.page.evaluate("""() => {
            const article = document.querySelector('[role="article"]');
            const root = {tag: 3, flags: 0}; root.stateNode = {current: root};
            const direction = {return: root, flags: 0, memoizedProps: {outgoing: true}};
            root.child = direction;
            const bad = {return: direction, flags: 0, memoizedProps: new Proxy({}, {
                getOwnPropertyDescriptor(_target, key) {
                    if (key === 'slideMessageRef') throw new Error('optional fixture failure');
                    return undefined;
                },
            })};
            direction.child = bad;
            const current = {return: bad, flags: 0,
                memoizedProps: {outgoing: false, currentMessageRef: {id: 'fixture-conflict'}}};
            bad.child = current;
            const direct = {return: current, flags: 0,
                memoizedProps: {messageId: 'fixture-conflict'}};
            current.child = direct;
            article.__reactFiber$fixture = direct;
        }""")

        article = (await self.page.evaluate(JS_RAW))["articles"][0]
        self.assertEqual(article["runtime_message_id"], "fixture-conflict")
        self.assertTrue(article["runtime_outgoing_conflict"])
        self.assertEqual(article["runtime_identity_source"], "react_current_message_v1")

        await self.open_fixture()
        await self.page.evaluate("""() => {
            const article = document.querySelector('[role="article"]');
            const root = {tag: 3, flags: 0}; root.stateNode = {current: root};
            const bad = {return: root, flags: 0, memoizedProps: new Proxy({}, {
                getOwnPropertyDescriptor(_target, key) {
                    if (key === 'slideMessageRef') throw new Error('optional fixture failure');
                    return undefined;
                },
            })};
            root.child = bad;
            const current = {return: bad, flags: 0,
                memoizedProps: {currentMessageRef: {id: 'fixture-fallback'}}};
            bad.child = current;
            const direct = {return: current, flags: 0,
                memoizedProps: {messageId: 'fixture-fallback'}};
            current.child = direct;
            article.__reactFiber$fixture = direct;
        }""")
        fallback = (await self.page.evaluate(JS_RAW))["articles"][0]
        self.assertFalse(any(key.startswith("runtime_") for key in fallback))

    async def test_throwing_reflection_falls_back_to_existing_reader(self) -> None:
        await self.open_fixture()
        baseline = await self.page.evaluate(JS_RAW)
        await self.page.evaluate("""() => {
            const article = document.querySelector('[role="article"]');
            Object.defineProperty(article, '__reactFiber$throwing', {value: new Proxy({}, {
                getOwnPropertyDescriptor() { throw new Error('fixture reflection failure'); },
            })});
        }""")
        fallback = await self.page.evaluate(JS_RAW)
        self.assertEqual(fallback["articles"], baseline["articles"])

"""Browser regressions for control text that is not message content."""

from __future__ import annotations

from test_reader import BrowserCase


class ControlTextBoundaryTest(BrowserCase):
    async def test_large_unlabelled_button_image_is_media_not_unknown(self):
        await self.open_fixture("thread_1to1.html")
        await self.page.evaluate("""() => {
            const art = document.createElement('div');
            art.setAttribute('role', 'article');
            art.innerHTML = '<div role="none"><div role="button">'
                + '<img alt="" width="236" height="280"></div></div>';
            document.querySelector('.pane').prepend(art);
        }""")

        event = (await self.read()).events[0]
        self.assertEqual((event.kind, event.media_label, event.text), ("media", "image", ""))

    async def test_unbound_large_image_is_not_promoted_to_media(self):
        await self.open_fixture("thread_1to1.html")
        await self.page.evaluate("""() => {
            const art = document.createElement('div');
            art.setAttribute('role', 'article');
            art.innerHTML = '<img alt="" width="236" height="280">';
            document.querySelector('.pane').prepend(art);
        }""")

        event = (await self.read()).events[0]
        self.assertEqual((event.kind, event.media_label, event.text), ("unknown", None, ""))

    async def test_hidden_descendant_text_is_not_message_content(self):
        await self.open_fixture("thread_1to1.html")
        before = await self.read()
        await self.page.evaluate("""() => {
            const text = document.querySelector('[role="article"] [dir="auto"]');
            text.innerHTML = 'visible<span style="display:none"> hidden control text</span>';
        }""")

        result = await self.read()
        self.assertEqual(result.events[0].text, "visible")
        self.assertNotEqual(result.events[0].text, before.events[0].text)

    async def test_emoji_reaction_button_with_a_text_leaf_stays_a_reaction(self):
        await self.open_fixture("thread_1to1.html")
        await self.page.evaluate("""() => {
            const article = document.querySelector('[role="article"]');
            const badge = document.createElement('div');
            badge.setAttribute('role', 'button');
            badge.innerHTML = '<span dir="auto">👍</span>';
            article.parentElement.append(badge);
        }""")

        result = await self.read()
        badge = next(event for event in result.events if event.text == "👍")
        self.assertEqual(badge.kind, "reaction")
        self.assertEqual(badge.evidence["target_order"], result.events[0].evidence["order"])
        self.assertEqual(result.events[0].evidence["reactions"], ["👍"])

    async def test_seen_in_a_scoped_button_is_a_receipt(self):
        cases = (
            ("Seen", "", ""),
            ("Seen by Fictional Contact", "", ""),
            ("Seen", ' dir="auto"', ""),
            ("Seen", "", '<span>control label</span>'),
        )
        for receipt, button_attrs, extra in cases:
            with self.subTest(receipt=receipt, button_attrs=button_attrs, extra=extra):
                await self.open_fixture("thread_1to1.html")
                await self.page.evaluate("""([receipt, buttonAttrs, extra]) => {
                    const button = document.createElement('div');
                    button.setAttribute('role', 'button');
                    if (buttonAttrs) button.setAttribute('dir', 'auto');
                    button.innerHTML = '<span dir="auto"></span>' + extra;
                    button.firstChild.textContent = receipt;
                    document.querySelector('.pane').append(button);
                }""", [receipt, button_attrs, extra])
                self.assertEqual((await self.read()).status, "receipts_visible")

    async def test_unbound_reaction_button_does_not_attach_across_content(self):
        await self.open_fixture("thread_1to1.html")
        await self.page.evaluate("""() => {
            const button = document.createElement('div');
            button.setAttribute('role', 'button');
            button.innerHTML = '<span dir="auto">👍</span>';
            document.querySelector('.pane').append(button);
        }""")

        result = await self.read()
        self.assertNotIn("👍", [event.text for event in result.events])
        self.assertTrue(all(not event.evidence.get("reactions") for event in result.events))

"""Dev tool — dump the live Instagram inbox DOM so selectors are written from
reality instead of guessed.

Start Chrome logged into Instagram first:
  chrome.exe --remote-debugging-port=9222

Then:  python probe.py
Writes probe.json.
"""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

from shadow.config import CDP_URL, IG_INBOX

JS = r"""() => {
    // Every anchor pointing at a DM thread, anywhere on the page.
    const anchors = [...document.querySelectorAll('a[href*="/direct/t/"]')].map(a => ({
        href: a.getAttribute('href'),
        text: (a.innerText || '').slice(0, 120),
        aria: a.getAttribute('aria-label'),
    }));

    // The thread-list container, if the aria-label we expect is there.
    const list = document.querySelector('[aria-label="Thread list"]');

    // First 8 rows of whatever the list holds, with every attribute, so we can
    // see what actually identifies a row.
    const rows = list
        ? [...list.querySelectorAll('[role="button"], [role="listitem"], li, a')]
            .slice(0, 8)
            .map(el => ({
                tag: el.tagName,
                role: el.getAttribute('role'),
                href: el.getAttribute('href'),
                aria: el.getAttribute('aria-label'),
                attrs: [...el.attributes].map(a => a.name + '=' + a.value).slice(0, 12),
                text: (el.innerText || '').split('\n').slice(0, 4),
            }))
        : [];

    return {
        url: location.href,
        title: document.title,
        logged_out: location.href.includes('/accounts/login'),
        thread_list_found: !!list,
        anchor_count: anchors.length,
        anchors: anchors.slice(0, 15),
        rows,
        // Any aria-label containing "thread"/"conversation", to catch a rename.
        aria_candidates: [...document.querySelectorAll('[aria-label]')]
            .map(el => el.getAttribute('aria-label'))
            .filter(l => /thread|conversation|chat|inbox/i.test(l))
            .slice(0, 20),
    };
}"""


async def main() -> None:
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            raise SystemExit(
                f"Cannot reach Chrome at {CDP_URL}: {e}\n"
                "Start Chrome with --remote-debugging-port=9222 and log into Instagram."
            )
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await page.goto(IG_INBOX, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(4000)  # let the thread list render

        data = await page.evaluate(JS)

    with open("probe.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"url:               {data['url']}")
    print(f"logged out:        {data['logged_out']}")
    print(f"thread list found: {data['thread_list_found']}")
    print(f"thread anchors:    {data['anchor_count']}")
    if data["anchors"]:
        print("first anchors:")
        for a in data["anchors"][:5]:
            print(f"   {a['href']}  {a['text'][:50]!r}")
    print("\nprobe.json written")


if __name__ == "__main__":
    asyncio.run(main())

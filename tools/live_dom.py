#!/usr/bin/env python3
"""Interactive live reader audit. Private paired DOM/screenshot evidence, no sends.

Run with --profile PATH --output-dir NEW_PRIVATE_PATH. Commands are JSON lines:
{"action":"capture"}, {"action":"open","index":0},
{"action":"scroll","direction":"up"}, {"action":"close"}.
Opening threads can clear unread badges; no composer input is available here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shadow.dom import JS_RAW, interpret_inbox, interpret_read, thread_id_from_url
from shadow.instagram import InstagramBrowser, _JS_PANE_CENTRE
from shadow.operations import private_destination

JS_STRUCTURE = r"""() => {
    const geometry = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return {tag: el.tagName, role: el.getAttribute('role'),
                left: r.left, top: r.top, width: r.width, height: r.height,
                scrollTop: el.scrollTop, scrollHeight: el.scrollHeight,
                clientHeight: el.clientHeight, overflowY: s.overflowY,
                display: s.display, flexDirection: s.flexDirection,
                contentVisibility: s.contentVisibility};
    };
    return {
        rows: [...document.querySelectorAll('[aria-label="Thread list"] div[role="button"]')]
            .filter(el => el.getBoundingClientRect().height >= 60 && el.getBoundingClientRect().height <= 90)
            .map(el => ({geometry: geometry(el), markup: el.outerHTML})),
        outside_images: [...document.querySelectorAll('img[alt]')]
            .filter(el => !el.closest('[role="article"], [aria-label="Thread list"]'))
            .map(el => ({geometry: geometry(el), markup: el.parentElement?.parentElement?.outerHTML})),
        outside_controls: [...document.querySelectorAll('[role="button"]')]
            .filter(el => !el.closest('[role="article"], [aria-label="Thread list"]'))
            .map(el => ({geometry: geometry(el), label: el.getAttribute('aria-label'),
                        text: el.innerText, markup: el.outerHTML,
                        ancestors: [el.parentElement, el.parentElement?.parentElement,
                                    el.parentElement?.parentElement?.parentElement]
                            .filter(Boolean).map(n => ({geometry: geometry(n),
                                articles: n.querySelectorAll('[role="article"]').length}))})),
        scrolls: [...document.querySelectorAll('div')]
            .filter(el => el.clientHeight >= 200 && el.scrollHeight > el.clientHeight + 40)
            .map(geometry),
        articles: [...document.querySelectorAll('[role="article"]')].map(el => ({
            geometry: geometry(el),
            markup: el.parentElement?.parentElement?.outerHTML,
            preceding: el.parentElement?.parentElement?.parentElement?.previousElementSibling?.outerHTML,
            ancestors: [el.parentElement, el.parentElement?.parentElement,
                        el.parentElement?.parentElement?.parentElement]
                .filter(Boolean).map(geometry),
            elements: [...el.querySelectorAll('a, img, video, svg, [role], [aria-label]')]
                .map(n => ({tag: n.tagName, role: n.getAttribute('role'),
                    label: n.getAttribute('aria-label'), alt: n.getAttribute('alt'),
                    href: n.getAttribute('href'), title: n.querySelector('title')?.textContent,
                    text: (n.innerText || '').slice(0, 200)})),
        })),
    };
}"""


async def audit(profile, output_dir):
    output = private_destination(output_dir)
    output.mkdir(mode=0o700)
    os.chmod(output, 0o700)
    browser = InstagramBrowser(profile_dir=profile)
    serial = 0

    async def capture():
        nonlocal serial
        browser._assert_on_site()
        raw = await browser.page.evaluate(JS_RAW)
        thread = thread_id_from_url(browser.page.url)
        result = interpret_read(raw, thread) if thread else interpret_inbox(raw)
        serial += 1
        stem = output / f"capture-{serial:03d}"
        shot = str(stem) + ".png"
        await browser.page.screenshot(path=shot)
        os.chmod(shot, 0o600)
        payload = {"raw": raw, "parsed": asdict(result), "screenshot": shot,
                   "structure": await browser.page.evaluate(JS_STRUCTURE)}
        descriptor = os.open(str(stem) + ".json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"capture": serial, "status": result.status, "parsed": asdict(result),
                          "screenshot": shot, "evidence": str(stem) + ".json"}, ensure_ascii=False), flush=True)

    try:
        initial = await browser.start()
        print(json.dumps({"started": initial.status, "detail": initial.detail}), flush=True)
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                command = json.loads(line)
                action = command["action"]
                if action == "close":
                    break
                if action == "open":
                    inbox = await browser.inbox()
                    index = command.get("index")
                    if type(index) is not int or index < 0 or index >= len(inbox.rows):
                        raise ValueError("Invalid current row index")
                    result = await browser.open_row(inbox.rows[index])
                    print(json.dumps({"opened": result.status}), flush=True)
                    await browser.page.wait_for_timeout(1000)
                elif action == "scroll":
                    direction = command.get("direction")
                    if direction not in ("up", "down"):
                        raise ValueError("Invalid direction")
                    browser._assert_on_site()
                    centre = await browser.page.evaluate(_JS_PANE_CENTRE)
                    if not centre:
                        raise ValueError("No verified scrollable message pane")
                    await browser.page.mouse.move(centre["x"], centre["y"])
                    await browser.page.mouse.wheel(0, -550 if direction == "up" else 550)
                    await browser.page.wait_for_timeout(1200)
                elif action != "capture":
                    raise ValueError("Unsupported diagnostic action")
                await capture()
            except Exception as exc:
                print(json.dumps({"error_type": type(exc).__name__}), flush=True)
    finally:
        await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    asyncio.run(audit(args.profile, args.output_dir))


if __name__ == "__main__":
    main()

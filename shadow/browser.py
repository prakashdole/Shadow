"""Browser — Playwright over CDP. DOM only, no vision, no fallbacks.

Three rules (see NOTES.md):

  1. CDP connect FAILS LOUD. The old code caught the connect error and fell
     back to a fresh persistent context with an empty profile, so it scraped
     the login page and reported "0 unread". There is no fallback here, and
     the login page raises.

  2. thread_id comes from the inbox scan, so every thread open is a URL
     navigation. The old code clicked row .nth(i), which goes stale the
     moment the list reorders — that is why it could not move between chats.

  3. NAVIGATION IS CONFINED TO instagram.com, in two layers: every URL is
     validated before we navigate, and a route guard aborts any top-level
     document load to another host (i.e. a redirect we did not ask for).
     This is why the bot can never end up typing a DM into some other site.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.async_api import Page, Route, async_playwright

from shadow.config import (
    CDP_URL, IG_HOME, IG_INBOX, IG_STORY, IG_THREAD, WORDS_PER_SEC,
)
from shadow.memory import Msg

log = logging.getLogger("shadow.browser")

SEL_TEXTBOX = 'div[contenteditable="true"][aria-label="Message"]'
SEL_MSG = '[dir="auto"]'


class NotLoggedIn(RuntimeError):
    pass


class OffSite(RuntimeError):
    """Refused to navigate outside instagram.com."""


# --- domain confinement ----------------------------------------------

# Exact hosts we will ever navigate to. Deliberately NOT a suffix match:
# "evilinstagram.com".endswith("instagram.com") is True, which is the classic
# way an allowlist like this gets bypassed.
ALLOWED_HOSTS = frozenset({"instagram.com", "www.instagram.com"})

# Hosts whose SUBRESOURCES (images, scripts, fonts) skip the route guard
# entirely. Instagram pulls hundreds of these per page; sending each one
# through Python would add seconds to every load. Navigations are still
# checked by check_url() before we ever request them.
_SKIP_GUARD = re.compile(
    r"^https?://([a-z0-9-]+\.)*"
    r"(instagram\.com|cdninstagram\.com|fbcdn\.net)"
    r"([/:?#]|$)",
    re.I,
)


def host_allowed(url: str) -> bool:
    """True if url is an https page on instagram.com."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme == "https" and (p.hostname or "").lower() in ALLOWED_HOSTS


def check_url(url: str) -> str:
    """Return url, or raise OffSite. Called before every navigation."""
    if not host_allowed(url):
        raise OffSite(
            f"refused to navigate to {url!r} - only https pages on "
            f"{sorted(ALLOWED_HOSTS)} are allowed"
        )
    return url


def _is_main_frame(req) -> bool:
    """True if this request would navigate the top-level page.

    Only a main-frame navigation can move us off Instagram; an iframe cannot.
    Playwright does not always expose `frame`, so treat an unknown frame as
    main-frame (fail closed - block rather than allow).
    """
    try:
        frame = req.frame
    except Exception:
        return True
    if frame is None:
        return True
    try:
        return frame.parent_frame is None
    except Exception:
        return True


@dataclass
class Thread:
    thread_id: str
    name: str
    last_text: str
    unread: bool


# Scan the inbox.
#
# There are NO thread anchors any more. The original design read thread_id out
# of `a[href*="/direct/t/"]`, but a live inbox has exactly two /direct/ links
# (inbox, requests) - rows are plain DIVs with no href at all, so threads()
# found nothing and raised. Rows are identified by height (~72px) and shape
# (name, preview, ·, age) inside [aria-label="Thread list"], and thread_id is
# learned later from the URL after clicking the row BY NAME.
#
# Clicking by name, not by index: the list reorders whenever a message
# arrives, which is exactly why the old .nth(i) click kept opening the wrong
# conversation.
_JS_INBOX = r"""() => {
    const list = document.querySelector('[aria-label="Thread list"]');
    if (!list) return [];
    const out = [], seen = new Set();
    for (const el of list.querySelectorAll('div')) {
        const r = el.getBoundingClientRect();
        if (r.height < 60 || r.height > 90) continue;      // a row, not a wrapper
        const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
        if (lines.length < 2) continue;
        const name = lines[0];
        if (!name || name.length > 40 || seen.has(name)) continue;
        // Chrome for the own-note composer and the section headers.
        if (/^(Messages|Requests|Your note|Try a fun|Primary|General)$/i.test(name)) continue;
        seen.add(name);

        const rest = lines.slice(1).filter(l => l !== 'Unread');
        const preview = rest[0] || '';

        // Unread detection. The previous rule was "no 'You:' prefix means
        // unread", which marked 11 of 14 threads unread when only 2 were:
        // Instagram writes "<name> sent an attachment." and "Reacted X to your
        // message" with no "You:" prefix, so shared reels and reactions all
        // looked like new inbound messages. Every one of those then cost a
        // thread open and a model call before the sender guard caught it.
        //
        // Rules, most authoritative first:
        //   - an explicit "Unread" token wins
        //   - "You: ..." is definitely mine, so definitely read
        //   - "<my own preview> sent an attachment." is also mine
        //   - a reaction to MY message means I sent the last real message
        const mineSent = /^You:/.test(preview);
        const iSentAttachment = /^You sent an attachment/i.test(preview);
        const reactedToMine = /Reacted .{0,4} to your message/i.test(preview);
        const unread = /(^|\s)Unread(\s|$)/.test(el.innerText)
            || !(mineSent || iSentAttachment || reactedToMine);

        out.push({
            thread_id: '',                 // filled in by open()
            name,
            last_text: rest.join(' ').slice(0, 200),
            unread,
            y: Math.round(r.top),
        });
    }
    // Top of the list first - Instagram sorts newest-first.
    return out.sort((a, b) => a.y - b.y).map(({y, ...rest}) => rest);
}"""

# Read the open thread. Sender is decided by horizontal position: Instagram
# right-aligns your own messages.
#
# The chat pane boundary is MEASURED, not guessed. A hardcoded `vw * 0.30`
# was calibrated on a narrow window; on a 2545px-wide one it put chatLeft at
# 764 while their messages start at x=535, so every incoming message was
# discarded and read() returned only your own. The date separators ("Tue
# 12:03") span the full pane width, which makes them a reliable ruler.
_JS_READ = r"""() => {
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;

    // The message pane: the scrollable container to the right of the thread
    // list. Finding it is what makes this fast - see the note below.
    let pane = null;
    for (const el of document.querySelectorAll('div')) {
        if (el.scrollHeight <= el.clientHeight + 40) continue;
        if (el.clientHeight < 200) continue;
        const r = el.getBoundingClientRect();
        if (r.left < vw * 0.15) continue;                 // thread list
        if (!pane || r.width > pane.getBoundingClientRect().width) pane = el;
    }

    // Only leaves INSIDE the pane, and no viewport-band filter.
    //
    // This is the fix that removed the need to scroll at all. Instagram keeps
    // ~90 messages in the DOM; the previous filter required
    // `top > vh*0.05 && top < vh*0.92`, which rejected every message scrolled
    // out of view (their rect.top is negative). So a read returned only the
    // ~10 on screen, and getting more meant 10 scroll round-trips at ~1.5s
    // each. Scoping to the pane instead gets everything already loaded in a
    // single pass, and negative tops still sort correctly (older = smaller).
    const root = pane || document;
    const leaves = [...root.querySelectorAll('[dir="auto"]')]
        .filter(el => el.querySelector('[dir="auto"]') === null);

    let paneL, paneR;
    if (pane) {
        const pr = pane.getBoundingClientRect();
        paneL = pr.left; paneR = pr.right;
    } else {
        // Fallback: widest full-width row (date separator), then the input box.
        let widest = 0; paneL = null; paneR = null;
        for (const el of leaves) {
            const r = el.getBoundingClientRect();
            if (r.width > widest && r.width > vw * 0.35 && r.right > vw * 0.5) {
                widest = r.width; paneL = r.left; paneR = r.right;
            }
        }
        if (paneL === null) {
            const box = document.querySelector(
                'div[contenteditable="true"][aria-label="Message"]');
            if (box) { const b = box.getBoundingClientRect(); paneL = b.left; paneR = b.right; }
        }
        if (paneL === null) { paneL = vw * 0.20; paneR = vw; }
    }

    const chatLeft = paneL - 20;              // small tolerance
    const chatMid  = (paneL + paneR) / 2;
    const inPane   = !!pane;

    const NOISE = [
        /^.{1,60}replied to (you|yourself)\s*([·•].*)?$/i,
        /^.{1,40} replied to you$/i,
        /^you replied to /i,
        /^replied to @/i,
        /^(edited|·|•|\.|Unread)$/i,
        /^\d{1,2}:\d{2}\s*(AM|PM)$/i,
        /^\d+[smhdw]$/i,
        /^(liked a message|you reacted|you unsent|message unavailable|Seen|Sent|Delivered|Active now)$/i,
        /^(Yesterday|Today)$/i,
        // Date separators - these span the pane and are the width ruler above,
        // so they must not come back as messages.
        /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}:\d{2}/i,
        /^(story unavailable|reel unavailable|post unavailable)$/i,
    ];
    const isNoise = t => {
        const f = (t || '').replace(/\s*\n\s*/g, ' ').trim();
        return !f || NOISE.some(r => r.test(f));
    };

    // Instagram renders emoji as <img alt="😭">, NOT as text. innerText skips
    // img alt entirely, so an emoji-only message read as "" and was dropped as
    // noise - which is why "Vansh | 😭" never appeared in the parsed output.
    // Walking the nodes and substituting alt text fixes it.
    const textOf = el => {
        let out = '';
        const walk = n => {
            if (n.nodeType === 3) { out += n.nodeValue; return; }
            if (n.nodeType !== 1) return;
            const tag = n.tagName;
            if (tag === 'IMG') {
                const a = n.getAttribute('alt');
                if (a) out += a;              // the emoji itself
                return;
            }
            if (tag === 'BR') { out += '\n'; return; }
            for (const c of n.childNodes) walk(c);
        };
        walk(el);
        return out.replace(/\s*\n\s*/g, ' ').trim();
    };

    // A shared reel/post/profile appears as a card with no text, so the model
    // would see nothing. Pull whatever identity the card exposes so it can at
    // least say "he sent a reel from @x" instead of staying silent.
    // A shared reel/post is a card, not text. Nemotron cannot watch it, so at
    // least tell it what arrived: "[reel from @handle]" beats a bare username.
    //
    // The card does NOT link to /reel/ or /p/ - looking for those found zero
    // cards on a thread full of them. What it actually contains is a link to
    // the ORIGINAL POSTER's profile (/lookmaxxeedd/) plus a role=button
    // wrapper whose text ends in "Clip" (reel) or "Post".
    const cardOf = el => {
        let box = el;
        for (let i = 0; i < 6 && box; i++) {
            const direct = box.querySelector && box.querySelector(
                'a[href*="/reel/"], a[href*="/p/"], a[href*="/tv/"]');
            if (direct) {
                const h = direct.getAttribute('href') || '';
                return {
                    kind: h.includes('/reel/') ? 'reel'
                        : h.includes('/tv/') ? 'video' : 'post',
                    who: '',
                };
            }
            // Profile-link card: the wrapper is a role=button and its text is
            // "<handle>Clip" or "<handle>Post".
            if (box.getAttribute && box.getAttribute('role') === 'button') {
                const txt = (box.innerText || '').replace(/\s+/g, ' ').trim();
                const m = txt.match(/^(.{1,40}?)(Clip|Post|Reel)$/i);
                if (m) {
                    const a = box.querySelector('a[href^="/"]');
                    const href = a ? (a.getAttribute('href') || '') : '';
                    const handle = href.replace(/^\/|\/$/g, '');
                    const word = m[2].toLowerCase();
                    return {
                        kind: word === 'post' ? 'post' : 'reel',
                        who: handle || m[1].trim(),
                    };
                }
            }
            box = box.parentElement;
        }
        return null;
    };

    const paneW = paneR - paneL;
    const rows = [];

    // Pass 1: collect every leaf in DOM order, laid out or not.
    //
    // Freshly loaded messages have width=0/height=0: Instagram inserts them
    // into the DOM before laying them out, and getBoundingClientRect() returns
    // all zeros. Requiring `width > 0` silently dropped 18 of 45 real messages
    // after each wheel, and Instagram virtualized them away before they were
    // ever measured. THAT was the "only 12 messages" bug - not the horizontal
    // filter. Sender is recovered in pass 2.
    for (const el of leaves) {
        const r = el.getBoundingClientRect();
        const laid = r.width > 0 && r.height > 0;
        if (laid && r.width > paneW * 0.9) continue;   // separators span it
        if (!inPane && laid && r.left <= chatLeft) continue;
        if (!inPane && laid && (r.top <= vh * 0.05 || r.top >= vh * 0.92)) continue;

        const text = textOf(el);
        if (isNoise(text)) continue;

        const card = cardOf(el);
        rows.push({
            text, laid,
            top: laid ? r.top : null,
            mine: laid ? ((r.left + r.width / 2) > chatMid) : null,
            type: card ? card.kind : 'text',
            shared: card ? (card.who || card.href) : null,
        });
    }

    // Pass 2: resolve the sender of rows that have no geometry.
    //
    // A message can be laid out on one read and unlaid on the next (a wheel
    // makes Instagram re-layout), so remember every sender we have ever
    // measured for a given text and reuse it. Guessing from neighbours alone
    // mislabeled real messages: "Kuch nai" and "Har chiz me kuch proove..."
    // are THEM when laid out, but became ME once they lost layout.
    window.__shSender = window.__shSender || new Map();
    for (const r of rows) {
        if (r.mine !== null) window.__shSender.set(r.text, r.mine);
    }
    // Bound the memo. It survives navigation (same page object), so over a
    // long run it would grow without limit. Keep the newest entries - Map
    // preserves insertion order, so dropping from the front drops the oldest.
    if (window.__shSender.size > 2000) {
        const keep = [...window.__shSender.entries()].slice(-1000);
        window.__shSender = new Map(keep);
    }
    for (let i = 0; i < rows.length; i++) {
        if (rows[i].mine !== null) continue;
        const remembered = window.__shSender.get(rows[i].text);
        if (remembered !== undefined) { rows[i].mine = remembered; continue; }
        // Never measured. Fall back to the nearest known neighbour, and to
        // 'them' if nothing is known - the bot then skips instead of replying
        // to what it thinks is its own message.
        let mine = null;
        for (let j = i + 1; j < rows.length && mine === null; j++) mine = rows[j].mine;
        if (mine === null)
            for (let j = i - 1; j >= 0 && mine === null; j--) mine = rows[j].mine;
        rows[i].mine = mine === null ? false : mine;
    }

    // Dedupe on sender+text; keep DOM order, which is chronological. Sorting by
    // `top` breaks once rows have no geometry.
    const out = [];
    const seen = new Set();
    for (const r of rows) {
        const sender = r.mine ? 'me' : 'them';
        const key = sender + '|' + r.text;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({
            sender,
            content: r.text,
            top: r.top === null ? 0 : Math.round(r.top),
            type: r.type,
            shared: r.shared,
        });
    }
    return out;
}"""

# True once the conversation pane exists. Used as the render signal for a
# read: element counts are satisfied by the inbox's own rows, this is not.
_JS_HAS_PANE = r"""() => {
    const vw = document.documentElement.clientWidth;
    for (const el of document.querySelectorAll('div')) {
        if (el.scrollHeight <= el.clientHeight + 40) continue;
        if (el.clientHeight < 200) continue;
        const r = el.getBoundingClientRect();
        if (r.left < vw * 0.15) continue;               // thread list
        return true;
    }
    return false;
}"""

# Centre of the message pane, for aiming wheel events.
_JS_PANE_CENTRE = r"""() => {
    const vw = document.documentElement.clientWidth;
    let p = null;
    for (const el of document.querySelectorAll('div')) {
        if (el.scrollHeight <= el.clientHeight + 40) continue;
        if (el.clientHeight < 200) continue;
        const r = el.getBoundingClientRect();
        if (r.left < vw * 0.15) continue;               // thread list
        if (!p || r.width > p.getBoundingClientRect().width) p = el;
    }
    if (!p) return null;
    const r = p.getBoundingClientRect();
    return {x: Math.round(r.left + r.width / 2),
            y: Math.round(r.top + r.height / 2)};
}"""

# Poll for `want` messages INSIDE the page. One CDP round-trip instead of one
# per poll - a Python-side loop cost ~40 round-trips and seconds of latency.
# Takes the reader as a string so there is only ever one copy of the parsing
# logic; two copies would drift and that drift would be a silent bug.
_JS_WAIT_FOR = r"""async ([readerSrc, want, budgetMs]) => {
    const read = eval('(' + readerSrc + ')');
    const t0 = performance.now();
    let n = read().length;
    while (performance.now() - t0 < budgetMs) {
        if (n >= want) break;
        await new Promise(r => setTimeout(r, 40));
        n = read().length;
    }
    return {count: n, waited: Math.round(performance.now() - t0)};
}"""

_JS_STORY_USERS = r"""() => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="/stories/"]')) {
        const parent = a.closest('button, [role="button"], li');
        const label = (parent?.getAttribute('aria-label') || '').toLowerCase();
        if (label.includes('seen') || label.includes('viewed')) continue;
        const m = (a.getAttribute('href') || '').match(/\/stories\/([^\/]+)/);
        if (m && m[1] !== 'highlights' && !out.includes(m[1])) out.push(m[1]);
    }
    return out;
}"""


class Browser:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self.page: Page | None = None

    # --- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            await self._pw.stop()
            raise RuntimeError(
                f"Cannot reach Chrome at {CDP_URL}: {e}\n"
                "Start Chrome with --remote-debugging-port=9222, logged into Instagram.\n"
                "There is no fallback on purpose — a fresh browser profile is not logged in, "
                "and the old code silently scraped the login page instead of failing."
            ) from e

        ctx = self._browser.contexts[0] if self._browser.contexts else None
        if ctx is None:
            raise RuntimeError("Chrome has no browser context")
        self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await self._install_guard()
        log.info("connected to Chrome at %s (confined to instagram.com)", CDP_URL)

    async def _install_guard(self) -> None:
        """Abort any top-level document load that leaves instagram.com.

        check_url() covers URLs we choose; this covers the ones we don't —
        a redirect, a meta refresh, a script-driven location change. Only
        document requests are blocked: killing subresources would break the
        page, and they cannot navigate us anywhere.
        """
        assert self.page

        async def guard(route: Route) -> None:
            req = route.request
            url = req.url
            if _SKIP_GUARD.match(url):
                await route.fallback()
                return
            if req.resource_type == "document" and req.is_navigation_request():
                # Only MAIN-frame navigations can take us off Instagram. An
                # iframe cannot, and Instagram's own auth-sync iframe lives on
                # facebook.com/instagram/login_sync - the old check matched it
                # (it is a document AND a navigation) and aborted it on every
                # single page load. Repeatedly blocking Meta's session sync
                # risks the login itself, which is the one thing we cannot
                # afford to break.
                if not _is_main_frame(req):
                    await route.fallback()
                    return
                log.error("BLOCKED off-site navigation to %s", url[:120])
                await route.abort()
                return
            await route.fallback()

        await self.page.route("**/*", guard)

        # A DM must never be typed into a page we did not choose. Cheap to
        # check, and it catches a same-tab hijack the route guard missed.
        self.page.on("framenavigated", self._on_nav)

    def _on_nav(self, frame) -> None:
        try:
            if frame.parent_frame is not None:
                return                      # iframes are not our concern
            url = frame.url
        except Exception:
            return
        if url and url != "about:blank" and not host_allowed(url):
            log.error("OFF-SITE: main frame is now at %s", url[:120])
            self._offsite = url

    def _assert_on_site(self) -> None:
        """Raise if the page drifted off instagram.com."""
        off = getattr(self, "_offsite", None)
        if off:
            raise OffSite(f"page navigated off instagram.com to {off!r}")
        if self.page and self.page.url and self.page.url != "about:blank":
            if not host_allowed(self.page.url):
                raise OffSite(f"page is at {self.page.url!r}, not instagram.com")

    async def close(self) -> None:
        # Do NOT close the context or the browser — it is the user's own Chrome.
        if self._pw:
            await self._pw.stop()

    async def _goto(self, url: str, pause: bool = True) -> None:
        """Navigate, refusing anything off instagram.com.

        `pause` adds a human-ish delay. Reads pass pause=False: the delay is
        there so the bot does not machine-gun page loads, and a read already
        waits on content afterwards. Keeping it on every read added 1-2.2s to
        a 1.6s operation for no benefit.
        """
        assert self.page
        check_url(url)                      # refuse off-site before requesting
        self._offsite = None
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if pause:
            await asyncio.sleep(random.uniform(1.0, 2.2))
        self._assert_on_site()              # and after, in case of redirect
        if "/accounts/login" in self.page.url:
            raise NotLoggedIn(
                f"Instagram redirected to login ({self.page.url}). "
                "Log in in the Chrome window and restart."
            )

    # --- inbox -------------------------------------------------------

    async def threads(self) -> list[Thread]:
        """Scan the inbox. Rows come back newest-first, thread_id empty.

        thread_id is not in the DOM (see _JS_INBOX), so it is filled in by
        open() after clicking the row.
        """
        assert self.page
        await self._goto(IG_INBOX)
        try:
            # The thread-list container, not thread links - there are none.
            await self.page.wait_for_selector(
                '[aria-label="Thread list"]', state="attached", timeout=15_000
            )
        except Exception:
            raise RuntimeError(
                "No thread list in the inbox after 15s. Either the page did not "
                "load or Instagram changed the layout. Run probe.py to see the DOM."
            )
        # The container appears before the rows are populated.
        try:
            await self.page.wait_for_function(
                "() => { const l = document.querySelector('[aria-label=\"Thread list\"]');"
                " if (!l) return false;"
                " return [...l.querySelectorAll('div')].some(d => {"
                "   const h = d.getBoundingClientRect().height;"
                "   return h > 60 && h < 90; }); }",
                timeout=10_000,
            )
        except Exception:
            log.warning("thread list rendered no rows - inbox may be empty")

        rows = await self.page.evaluate(_JS_INBOX)
        out = [Thread(r["thread_id"], r["name"], r["last_text"], r["unread"])
               for r in rows]
        log.info("inbox: %d threads, %d unread", len(out), sum(t.unread for t in out))
        return out

    async def open(self, thread: Thread) -> str:
        """Click a thread row BY NAME and return its thread_id from the URL.

        Not by row index: the list reorders whenever a message arrives, which
        is why the original .nth(i) click kept opening the wrong conversation.
        Mutates thread.thread_id so the caller can navigate by URL later.
        """
        assert self.page
        if thread.thread_id:
            await self._goto(IG_THREAD.format(thread_id=thread.thread_id))
            return thread.thread_id

        if "/direct/inbox" not in self.page.url:
            await self._goto(IG_INBOX)
            await self.page.wait_for_selector(
                '[aria-label="Thread list"]', state="attached", timeout=15_000
            )

        # Escape quotes so a name like O'Brien cannot break the selector.
        safe = thread.name.replace("\\", "\\\\").replace('"', '\\"')
        row = self.page.locator(
            f'[aria-label="Thread list"] div:has-text("{safe}")'
        ).last
        await row.click(timeout=10_000)
        await self.page.wait_for_url("**/direct/t/**", timeout=10_000)
        self._assert_on_site()

        tid = thread_id_from_url(self.page.url)
        if not tid:
            raise RuntimeError(
                f"clicked {thread.name!r} but URL has no thread id: {self.page.url}"
            )
        thread.thread_id = tid
        log.info("opened %r -> thread %s", thread.name, tid)
        return tid

    async def read(self, thread_id: str, want: int = 0) -> list[Msg]:
        """Read the messages of a thread.

        want=0 reads only what is rendered (~10 messages, one round-trip).
        want=N wheels the pane up until N are loaded - see _load_history().

        Skips navigation when we are already in the thread, which is the case
        right after open() clicked the row.
        """
        assert self.page
        if thread_id not in self.page.url:
            # pause=False: a read waits on content anyway, so the human-ish
            # delay would add 1-2.2s to a 1.6s operation for nothing.
            await self._goto(IG_THREAD.format(thread_id=thread_id), pause=False)

        # Wait for the MESSAGE PANE, not for elements to exist.
        #
        # Three traps here, each of which cost real debugging time:
        #   - wait_for_selector's default state="visible" checks visibility on
        #     the FIRST match, and IG's first [dir=auto] span is zero-size, so
        #     it timed out while 90+ messages sat in the DOM
        #   - state="attached" resolves instantly on that same empty span, so
        #     the read fired before anything rendered and returned 0 messages
        #   - counting [dir=auto] elements is satisfied by the INBOX's own
        #     thread-list rows, so right after open() clicked a row the wait
        #     passed immediately and the read scraped sidebar chrome
        #     ("Your messages", "Also from Meta") instead of messages
        # The scrollable pane only exists once the conversation has rendered,
        # which makes it the honest signal.
        try:
            await self.page.wait_for_function(_JS_HAS_PANE, timeout=15_000)
        except Exception:
            log.error("thread %s did not render a message pane", thread_id)
            return []
        if thread_id not in self.page.url:
            log.error("wrong thread open: wanted %s, got %s", thread_id, self.page.url)
            return []

        if want > 0:
            await self._load_history(want)

        raw = await self.page.evaluate(_JS_READ)
        return [Msg(r["sender"], self._label(r)) for r in raw]

    async def _load_history(self, want: int) -> None:
        """Wheel the message pane up until `want` messages are loaded.

        Three findings, each worth a couple of seconds:

        1. Instagram only puts ~10 messages in the DOM and loads more on
           TRUSTED input. Setting `pane.scrollTop` in JS does nothing - the
           pane has ~300px of range and never grows. Real wheel events work.

        2. Every wheel dispatch blocks ~2s waiting for scroll-settle, in raw
           CDP as much as in `page.mouse.wheel`. Four sequential wheels = 8s.
           Sent CONCURRENTLY they overlap and cost ~2s total.

        3. Polling from Python is a CDP round-trip per check. _JS_WAIT_FOR
           polls in-page instead: one round-trip, ~1.4s.

        Net: ~4.5s for 20+ messages, down from 21s.
        """
        assert self.page
        centre = await self.page.evaluate(_JS_PANE_CENTRE)
        if not centre:
            log.warning("no scrollable message pane; reading what is rendered")
            return

        cdp = await self.page.context.new_cdp_session(self.page)
        try:
            wheel = {
                "type": "mouseWheel", "x": centre["x"], "y": centre["y"],
                "deltaX": 0, "deltaY": -2500, "pointerType": "mouse",
            }
            await asyncio.gather(
                *(cdp.send("Input.dispatchMouseEvent", wheel) for _ in range(4))
            )
            res = await self.page.evaluate(_JS_WAIT_FOR, [_JS_READ, want, 2500])
            log.info("history: %d messages after %dms", res["count"], res["waited"])
        finally:
            await cdp.detach()

    @staticmethod
    def _label(r: dict) -> str:
        """Text for the model. A shared reel has no text of its own, so name it
        rather than handing the model an empty string."""
        content = (r.get("content") or "").strip()
        kind = r.get("type", "text")
        if kind == "text":
            return content
        who = r.get("shared") or ""
        tag = f"[{kind}" + (f" from @{who}]" if who and "/" not in who else "]")
        # The card's only text is usually the poster's handle, which the tag
        # already names - "[reel from @x] x" reads as a stutter to the model.
        if who and content.lower() == who.lower():
            return tag
        return f"{tag} {content}".strip()

    # --- sending -----------------------------------------------------

    async def send(self, text: str) -> bool:
        """Type into the open thread and press Enter. Verifies the row count grew."""
        assert self.page
        # Never type into a page we did not navigate to ourselves.
        self._assert_on_site()
        box = self.page.locator(SEL_TEXTBOX).first
        try:
            await box.wait_for(state="visible", timeout=10_000)
        except Exception:
            log.error("message input not found — is a thread actually open?")
            return False

        before = await self.page.evaluate(
            f"document.querySelectorAll('{SEL_MSG}').length"
        )
        await box.click()
        await box.type(text, delay=random.randint(45, 90))
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await self.page.keyboard.press("Enter")

        try:
            await self.page.wait_for_function(
                f"document.querySelectorAll('{SEL_MSG}').length > {before}",
                timeout=8_000,
            )
        except Exception:
            log.error("send not confirmed (row count did not grow): %r", text[:60])
            return False
        log.info("sent: %s", text[:80])
        return True

    async def send_burst(self, msgs: list[str]) -> bool:
        """Send several messages in a row, the way a person actually texts."""
        ok = False
        for i, m in enumerate(msgs):
            if i:
                await asyncio.sleep(random.uniform(1.2, 3.0))
            else:
                await asyncio.sleep(max(1.0, len(m.split()) / WORDS_PER_SEC * 0.5))
            if not await self.send(m):
                return ok
            ok = True
        return ok

    # --- stories -----------------------------------------------------

    async def stories(self, limit: int) -> int:
        """View up to `limit` unwatched stories. Returns how many were opened."""
        assert self.page
        await self._goto(IG_HOME)
        try:
            await self.page.wait_for_selector('a[href*="/stories/"]', timeout=10_000)
        except Exception:
            log.info("no story tray on home")
            return 0
        users = await self.page.evaluate(_JS_STORY_USERS)
        viewed = 0
        for u in users[:limit]:
            try:
                await self._goto(IG_STORY.format(username=u))
                await asyncio.sleep(random.uniform(3, 7))  # actually watch it
                viewed += 1
            except Exception as e:
                log.warning("story %s failed: %s", u, e)
        log.info("viewed %d/%d stories", viewed, len(users))
        return viewed


def thread_id_from_url(url: str) -> str | None:
    m = re.search(r"/direct/t/(\d+)", url)
    return m.group(1) if m else None

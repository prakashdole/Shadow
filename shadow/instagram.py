"""The browser adapter: `InstagramBrowser` implements `BrowserPort`.

Everything platform-facing lives here; every decision lives in `shadow.dom`.
The rules this class must never break:

* One browser, one context, one Instagram tab, and that tab is chosen by
  ``host_allowed`` - never "the first tab".
* Every navigation is checked before it is requested AND after it happened, and
  each one is counted and reported. Steady-state reads are in-page and cost 0.
* Exact thread identity: a thread id is digits parsed from ``/direct/t/<digits>/``
  and is compared with ``==``. ``12`` is not ``123``.
* There is exactly one send primitive, `_send_on_page`, and it returns
  ``failed_before_submit`` only while nothing has been submitted. After Enter it
  can only return ``confirmed`` or ``uncertain`` - never a clean failure.
* Never close the owner's browser, context or tabs. Stopping Playwright is all
  that `close()` does.
"""

from __future__ import annotations

import asyncio
import logging
import json
import re
import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Route, async_playwright

from shadow.browser import _is_main_frame, check_url, host_allowed
from shadow.contracts import (
    CheckPoint, InboxResult, InboxRow, ReadResult, SendCheck, SendResult,
)
from shadow.dom import (
    JS_CLEAR_COMPOSER, JS_COMPOSER, JS_FIND_PANE, JS_RAW, JS_ROW_LINES, interpret_inbox, interpret_read,
    normalize, same_thread, thread_id_from_url,
)
from shadow.settings import Settings

log = logging.getLogger("shadow.instagram")

# PROPOSED SETTING: cdp_url - loopback only, refused otherwise.
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

# PROPOSED SETTING: send_confirm_timeout - seconds to wait for the new bubble.
SEND_CONFIRM_TIMEOUT = 8.0
# PROPOSED SETTING: send_confirm_poll_ms - in-page read interval while waiting.
SEND_CONFIRM_POLL_MS = 250

IG_INBOX = "https://www.instagram.com/direct/inbox/"
IG_THREAD = "https://www.instagram.com/direct/t/{thread_id}/"

# Attached to a bubble that reports a delivery failure. Read through the
# production reader, so a visible error is evidence rather than a guess.
_ERROR_TEXT = re.compile(
    r"couldn'?t send|failed to send|not delivered|tap to retry", re.I
)

_SKIP_GUARD = re.compile(
    r"^https?://([a-z0-9-]+\.)*"
    r"(instagram\.com|cdninstagram\.com|fbcdn\.net)"
    r"([/:?#]|$)",
    re.I,
)

# Click the one row whose label matches, and only if it is unique. The label is
# passed as an argument, never interpolated into a selector, so owner-controlled
# text cannot break out of it.
_JS_CLICK_ROW = r"""(label) => {
""" + JS_ROW_LINES + r"""
    const list = document.querySelector('[aria-label="Thread list"]');
    if (!list) return 0;
    const band = [...list.querySelectorAll('div[role="button"]')].filter(el => {
        const h = el.getBoundingClientRect().height;
        return h >= 60 && h <= 90;
    });
    const rows = band.filter(el => !band.some(o => o !== el && el.contains(o)));
    const hits = rows.filter(el => {
        const lines = rowLines(el);
        return lines[0] === label;
    });
    if (hits.length !== 1) return hits.length;
    hits[0].click();
    return 1;
}"""

_JS_ON_THREAD = r"""() => /^\/direct\/t\/\d+\/?$/.test(location.pathname)"""
_JS_THREAD_OPENED = r"""(previous) => {
    const match = location.pathname.match(/^\/direct\/t\/(\d+)\/?$/);
    return !!match && (!previous || match[1] !== previous);
}"""

_JS_PANE_CENTRE = r"""() => {
""" + JS_FIND_PANE + r"""
    const pane = findMessagePane();
    if (!pane) return null;
    const r = pane.getBoundingClientRect();
    return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
}"""

# One browser round-trip; DOM mutations and periodic geometry checks share a
# deadline. Timer sampling also works in headless/background tabs without rAF.
_JS_WAIT_RAW = r"""async ([source, thread, expectedPath, budget, quiet, sample, scroll]) => {
""" + JS_FIND_PANE + r"""
    const read = eval('(' + source + ')');
    const pane = scroll ? findMessagePane() : null;
    if (scroll && !pane) return {raw: read(), reason: 'pane_missing'};
    const signature = raw => JSON.stringify([raw.path, raw.rows, raw.header, raw.parser_profile,
        raw.composer_text, raw.pane,
        thread ? raw.articles.map(a => [a.text, a.media_label, a.reply_to_text, a.edited,
            a.left, a.width, a.laid]) : null, thread ? raw.outside : null, raw.loading]);
    const before = read();
    const beforeSignature = signature(before);
    const beforeScroll = pane?.scrollTop;
    let changed = !scroll;
    return await new Promise(resolve => {
        let previous = null, stableSince = performance.now(), timer = null, done = false;
        const finish = (raw, reason) => {
            if (done) return;
            done = true; observer.disconnect(); clearTimeout(timer); clearTimeout(deadline);
            resolve({raw, reason, moved: !!pane && pane.scrollTop !== beforeScroll});
        };
        const check = () => {
            if (done) return;
            clearTimeout(timer);
            const raw = read(), now = performance.now(), sig = signature(raw);
            if (window.__shadowHistoryGuard?.interrupted) {
                finish(raw, 'owner_interrupted'); return;
            }
            if (scroll && (findMessagePane() !== pane || !pane.isConnected)) {
                finish(raw, 'pane_replaced'); return;
            }
            if (raw.path !== expectedPath) { finish(raw, 'path_changed'); return; }
            if (/^\/(?:accounts\/login|challenge|checkpoint|suspended)/.test(raw.path)
                    || raw.seen) { finish(raw, 'status'); return; }
            if (sig !== beforeSignature) changed = true;
            const moved = !!pane && pane.scrollTop !== beforeScroll;
            const ready = raw.list_present && (thread ? (raw.pane_present
                && !raw.loading && raw.articles.length > 0
                && raw.articles.every(a => a.laid && (a.text || a.media_label)))
                : (raw.rows.length > 0 || raw.inbox_empty_verified));
            if (!ready || sig !== previous) stableSince = now;
            previous = ready ? sig : null;
            if ((changed || (moved && !raw.at_visual_start)) && ready && now - stableSince >= quiet) {
                finish(raw, 'settled'); return;
            }
            timer = setTimeout(check, sample);
        };
        // Coalesce a mutation burst; timers still sample geometry changes.
        const observer = new MutationObserver(() => {
            clearTimeout(timer); timer = setTimeout(check, 0);
        });
        const watchRoot = thread ? (pane || findMessagePane() || document.body)
            : (document.querySelector('[aria-label="Thread list"]') || document.body);
        observer.observe(watchRoot.parentElement || watchRoot, {subtree: true, childList: true,
            characterData: true, attributes: true,
            attributeFilter: ['aria-busy', 'aria-pressed', 'style', 'class',
                              'role', 'aria-label', 'aria-labelledby', 'dir', 'alt', 'href']});
        const deadline = setTimeout(() => finish(read(), 'timeout'), budget);
        check();
        if (scroll && !done) {
            // Keep viewport overlap; dispatch only after observation is armed.
            pane.scrollBy({top: -Math.max(1, pane.clientHeight * 0.8), behavior: 'instant'});
        }
    });
}"""

_JS_HISTORY_BEGIN = r"""() => {
""" + JS_FIND_PANE + r"""
    const pane = findMessagePane();
    if (!pane || window.__shadowHistoryGuard) return false;
    const guard = {pane, top: pane.scrollTop, extent: pane.scrollHeight,
        path: location.pathname, interrupted: false};
    guard.onInput = event => { if (event.isTrusted) guard.interrupted = true; };
    guard.types = ['wheel', 'pointerdown', 'keydown', 'beforeinput', 'focusin'];
    for (const type of guard.types) document.addEventListener(type, guard.onInput, true);
    guard.onBlur = () => { guard.interrupted = true; };
    guard.onVisibility = () => { guard.interrupted = true; };
    window.addEventListener('blur', guard.onBlur, true);
    document.addEventListener('visibilitychange', guard.onVisibility, true);
    window.__shadowHistoryGuard = guard;
    return true;
}"""

_JS_HISTORY_END = r"""async ([source, budget, quiet, sample, anchors]) => {
""" + JS_FIND_PANE + r"""
    const guard = window.__shadowHistoryGuard;
    if (!guard) return {restored: false, reason: 'guard_missing'};
    try {
        if (guard.interrupted) return {restored: false, reason: 'owner_interrupted'};
        if (location.pathname !== guard.path || findMessagePane() !== guard.pane
                || !guard.pane.isConnected) return {restored: false, reason: 'view_replaced'};
        const pane = guard.pane;
        const reversed = getComputedStyle(pane).flexDirection === 'column-reverse';
        const target = guard.top + (reversed ? 0 : pane.scrollHeight - guard.extent);
        pane.scrollTo({top: target, behavior: 'instant'});
        const read = eval('(' + source + ')');
        const deadline = performance.now() + budget;
        let previous = null, since = performance.now();
        do {
            if (guard.interrupted) return {restored: false, reason: 'owner_interrupted'};
            if (location.pathname !== guard.path || findMessagePane() !== pane || !pane.isConnected)
                return {restored: false, reason: 'view_replaced'};
            const raw = read();
            if (raw.seen) return {restored: false, reason: 'receipts_visible'};
            const laid = raw.articles.filter(a => a.laid);
            const signature = JSON.stringify(laid.map(a => [a.text, a.media_label, a.reply_to_text]));
            const keys = laid.map(a => JSON.stringify([a.text, a.media_label, a.reply_to_text]));
            const restoredAnchors = keys.some((_, i) => anchors.every((key, j) => key === keys[i + j]));
            if (signature !== previous || !restoredAnchors || raw.loading) since = performance.now();
            previous = signature;
            if (restoredAnchors && !raw.loading && performance.now() - since >= quiet)
                return {restored: Math.abs(pane.scrollTop - target) <= 2,
                        reason: 'rendered_content', raw};
            await new Promise(resolve => setTimeout(resolve, sample));
        } while (performance.now() < deadline);
        return {restored: false, reason: 'content_unsettled'};
    } finally {
        for (const type of guard.types) document.removeEventListener(type, guard.onInput, true);
        window.removeEventListener('blur', guard.onBlur, true);
        document.removeEventListener('visibilitychange', guard.onVisibility, true);
        delete window.__shadowHistoryGuard;
    }
}"""

# The nav profile link, if the header rendered one. Returned to Python only to
# be compared with the configured account; never returned, never logged.
_JS_NAV_HANDLE = r"""() => {
    for (const a of document.querySelectorAll('a[href]')) {
        const name = (a.getAttribute('aria-label') || a.innerText || '').trim();
        const href = a.getAttribute('href') || '';
        if (name.toLowerCase() === 'profile' && /^\/[^/]+\/?$/.test(href)) {
            return href.replace(/^\/|\/$/g, '');
        }
    }
    return null;
}"""


def refuse_cdp_url(url: str) -> None:
    """A CDP endpoint that is not loopback could be someone else's browser."""
    host = (urlparse(url).hostname or "").lower()
    if host not in _LOOPBACK:
        raise ValueError(f"cdp_url host {host!r} is not loopback; refusing")


def _owner_positions(result: ReadResult) -> list[int]:
    return [e.position for e in result.events if e.sender == "owner"]


async def _send_on_page(
    page, thread_id: str, text: str, check: SendCheck,
    timeout: float = SEND_CONFIRM_TIMEOUT,
) -> SendResult:
    """The ONLY send primitive. No host check, so a fixture can drive it.

    Everything before Enter may return ``failed_before_submit``; after Enter it
    may only return ``confirmed`` or ``uncertain``.
    """
    want = normalize(text)

    async def read() -> ReadResult:
        return interpret_read(await page.evaluate(JS_RAW), thread_id)

    def matches(result: ReadResult) -> list[int]:
        return [e.position for e in result.events
                if e.sender == "owner" and normalize(e.text) == want]

    baseline = await read()
    if baseline.status != "ok":
        return SendResult(status="failed_before_submit", thread_id=thread_id,
                          error_code=f"read_{baseline.status}")
    if not same_thread(baseline.thread_id, thread_id):
        return SendResult(status="failed_before_submit", thread_id=thread_id,
                          error_code="wrong_thread")

    before = matches(baseline)
    owners = _owner_positions(baseline)
    last_before = owners[-1] if owners else -1

    # (a) a composer already holding the owner's typing is never overwritten.
    if baseline.composer_text:
        return SendResult(status="failed_before_submit", thread_id=thread_id,
                          error_code="manual_text",
                          evidence={"composer_non_empty": True})

    # (c) nothing typed yet.
    if not await check("before_type"):
        return SendResult(status="failed_before_submit", thread_id=thread_id,
                          error_code="aborted_before_type")

    await page.evaluate(JS_COMPOSER)
    await page.keyboard.insert_text(text)

    # (e) aborted between typing and Enter: the composer must not keep the text.
    if not await check("before_enter"):
        selected = await page.evaluate(JS_CLEAR_COMPOSER)
        if selected is not False:
            await page.keyboard.press("Backspace")
        remaining = (await page.evaluate(JS_COMPOSER) or "").strip()
        if remaining:
            return SendResult(status="uncertain", thread_id=thread_id,
                              error_code="composer_not_cleared",
                              evidence={"composer_remaining_chars": len(remaining)})
        return SendResult(status="failed_before_submit", thread_id=thread_id,
                          error_code="aborted_before_enter",
                          evidence={"composer_cleared": True})

    # (f) submitted. Nothing below may report a clean failure.
    await page.keyboard.press("Enter")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        await asyncio.sleep(SEND_CONFIRM_POLL_MS / 1000)
        result = await read()
        if result.status != "ok":
            return SendResult(status="uncertain", thread_id=thread_id,
                              error_code=f"read_{result.status}",
                              evidence={"matches_before": len(before)})
        # A visible failure marker beats any bubble evidence.
        for event in result.events:
            if _ERROR_TEXT.search(event.text or ""):
                return SendResult(status="uncertain", thread_id=thread_id,
                                  error_code="error_marker",
                                  evidence={"matches_before": len(before),
                                            "matches_after": len(matches(result))})
        after = matches(result)
        if len(after) == len(before) + 1:
            new_position = next(p for p in after if p not in before)
            owners = _owner_positions(result)
            if owners and owners[-1] == new_position and new_position > last_before:
                return SendResult(
                    status="confirmed", thread_id=thread_id,
                    matched_position=new_position,
                    evidence={"matches_before": len(before),
                              "matches_after": len(after)},
                )
        if loop.time() >= deadline:
            return SendResult(status="uncertain", thread_id=thread_id,
                              error_code="no_confirmation",
                              evidence={"matches_before": len(before),
                                        "matches_after": len(matches(result)),
                                        "events": len(result.events)})


class InstagramBrowser:
    """`BrowserPort` over a dedicated, already-running Chrome on loopback."""

    def __init__(self, cdp_url: str = DEFAULT_CDP_URL,
                 expected_account: str | None = None, *,
                 profile_dir: str | Path | None = None, headless: bool = False,
                 settings: Settings | None = None) -> None:
        refuse_cdp_url(cdp_url)
        if headless and profile_dir is None:
            raise ValueError("Headless mode requires a dedicated app-owned profile")
        self.profile_dir = Path(profile_dir).expanduser().resolve() if profile_dir else None
        if self.profile_dir is not None:
            from shadow.operations import private_destination

            private_destination(self.profile_dir, must_be_new=False)
            # Never launch with the user's ordinary Chrome profile.
            forbidden = {"default", "user data", "google-chrome", "chromium", "google", "chrome"}
            if any(part.casefold() in forbidden for part in self.profile_dir.parts):
                raise ValueError("Use a dedicated Shadow profile, not a personal browser profile")
        self.headless = headless
        self.settings = settings or Settings()
        self._owned_context = None
        self.cdp_url = cdp_url
        # A comparison value only. Never logged, never returned in a result.
        self._expected_account = expected_account
        self._account_detail = ""
        self._pw = None
        self._browser = None
        self.page = None
        self._offsite: str | None = None

    # --- lifecycle ---------------------------------------------------

    async def start(self) -> InboxResult:
        """Bind to the single Instagram tab. Returns a status, never raises."""
        self._pw = await async_playwright().start()
        if self.profile_dir is not None:
            try:
                self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.profile_dir, 0o700)
                self._owned_context = await self._pw.chromium.launch_persistent_context(
                    self.profile_dir, headless=self.headless, viewport={"width": 1280, "height": 900},
                    locale="en-US", accept_downloads=False, chromium_sandbox=True,
                )
                pages = [page for page in self._owned_context.pages if host_allowed(page.url)]
                if not pages:
                    self.page = await self._owned_context.new_page()
                    await self._install_guard()
                    await self.page.goto(IG_INBOX, wait_until="domcontentloaded")
                    pages = [self.page]
                if len(pages) != 1:
                    return InboxResult("dom_unknown", detail="owned_profile_multiple_instagram_tabs")
                if self.page is not pages[0]:
                    self.page = pages[0]
                    await self._install_guard()
                await self._wait_readable()
                failed = await self._check_account()
                if failed:
                    return failed
                return interpret_inbox(await self.page.evaluate(JS_RAW), extra_detail=self._detail())
            except Exception:
                await self.close()
                return InboxResult("dom_unknown", detail="owned_browser_start_failed")
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as exc:
            await self._pw.stop()
            self._pw = None
            return InboxResult(status="dom_unknown",
                               detail=f"cdp_connect_failed:{type(exc).__name__}")

        pages = []
        for context in self._browser.contexts:
            for page in context.pages:
                try:
                    url = page.url or ""
                except Exception:
                    continue
                if host_allowed(url):
                    pages.append(page)
        if len(pages) != 1:
            await self._pw.stop()
            self._pw = None
            return InboxResult(
                status="dom_unknown",
                detail=f"expected_exactly_one_instagram_page_found_{len(pages)}",
            )
        self.page = pages[0]
        await self._install_guard()
        log.info("bound to the single instagram.com tab (identity never logged)")

        await self._wait_readable()
        failed = await self._check_account()
        if failed:
            return failed
        return interpret_inbox(await self.page.evaluate(JS_RAW),
                                 extra_detail=self._detail())

    async def close(self) -> None:
        """Stop Playwright only. The owner's browser, context and tabs stay."""
        if self._owned_context is not None:
            await self._owned_context.close()
            self._owned_context = None
        if self._pw:
            await self._pw.stop()
            self._pw = None
        self._browser = None
        self.page = None

    async def _install_guard(self) -> None:
        assert self.page

        async def guard(route: Route) -> None:
            request = route.request
            url = request.url
            if _SKIP_GUARD.match(url):
                await route.fallback()
                return
            if request.resource_type == "document" and request.is_navigation_request():
                # Only a MAIN-frame navigation can take us off Instagram. An
                # iframe cannot, and blocking the third-party session-sync iframe
                # on every load risks the login itself.
                if not _is_main_frame(request):
                    await route.fallback()
                    return
                log.error("blocked off-site main-frame navigation to %s", url[:120])
                self._offsite = url
                await route.abort()
                return
            await route.fallback()

        await self.page.route("**/*", guard)
        self.page.on("framenavigated", self._on_nav)

    def _on_nav(self, frame) -> None:
        try:
            if frame.parent_frame is not None:
                return
            url = frame.url
        except Exception:
            return
        if url and url != "about:blank" and not host_allowed(url):
            log.error("main frame is off-site")
            self._offsite = url

    def _detail(self) -> str:
        return self._account_detail

    async def _check_account(self) -> InboxResult | None:
        """Recheck the account before every read and submission checkpoint."""
        if self._expected_account is None:
            return None
        screen = interpret_inbox(await self.page.evaluate(JS_RAW))
        if screen.status in ("login_required", "challenge", "receipts_visible"):
            return screen
        found = await self.page.evaluate(_JS_NAV_HANDLE)
        if not found:
            self._account_detail = "account_unverified"
            return InboxResult(status="wrong_account", detail=self._account_detail)
        if found != self._expected_account:
            return InboxResult(status="wrong_account", detail="account_handle_differs")
        self._account_detail = ""
        return None

    # --- navigation --------------------------------------------------

    async def _wait_readable(self, *, thread: bool = False) -> bool:
        """Wait for stable rendered structure, not just an SPA URL change."""
        from shadow.dom import page_status
        self._assert_on_site()
        raw = await self.page.evaluate(JS_RAW)
        settled = await self.page.evaluate(_JS_WAIT_RAW, [
            JS_RAW, thread, raw.get('path'), self.settings.dom_read_timeout_ms,
            self.settings.dom_settle_ms, self.settings.dom_sample_ms, False,
        ])
        self._assert_on_site()
        return (settled['reason'] == 'settled' or bool(settled['raw'].get('seen'))
                or bool(page_status(settled['raw'].get('path', ''))))

    async def _goto(self, url: str) -> int:
        check_url(url)                      # refuse off-site before requesting
        self._offsite = None
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._assert_on_site()              # and after, in case of a redirect
        return 1

    def _assert_on_site(self) -> None:
        if self._offsite:
            raise RuntimeError("page navigated off instagram.com")
        url = self.page.url if self.page else ""
        if url and url != "about:blank" and not host_allowed(url):
            raise RuntimeError("page is at an off-site url")

    # --- BrowserPort -------------------------------------------------

    async def inbox(self) -> InboxResult:
        assert self.page
        raw = await self.page.evaluate(JS_RAW)
        failed = await self._check_account()
        if failed:
            return failed
        # Steady state: the thread list is already rendered, so read in page.
        if "/direct/" in (self.page.url or "") and raw.get("list_present"):
            if not raw.get('rows') and not raw.get('inbox_empty_verified'):
                await self._wait_readable()
                raw = await self.page.evaluate(JS_RAW)
            return interpret_inbox(raw, extra_detail=self._detail())
        navigations = await self._goto(IG_INBOX)
        await self._wait_readable()
        failed = await self._check_account()
        if failed:
            return failed
        try:
            await self.page.wait_for_selector(
                '[aria-label="Thread list"]', state="attached", timeout=15_000
            )
        except Exception:
            return InboxResult(status="dom_unknown", navigations_used=navigations,
                               detail="thread_list_never_rendered")
        return interpret_inbox(await self.page.evaluate(JS_RAW),
                                 extra_detail=self._detail())

    async def open_row(self, row: InboxRow) -> ReadResult:
        assert self.page
        failed = await self._check_account()
        if failed:
            return ReadResult(status=failed.status, detail=failed.detail)
        # The list reorders and unread flags change between reads (P00b Q10), so
        # the row is re-read immediately before the click and must still be
        # unique. A display label is never an identity.
        if not row.label_is_unique:
            return ReadResult(status="ambiguous_identity",
                              detail="label_not_unique_across_rows")
        raw = await self.page.evaluate(JS_RAW)
        inbox = interpret_inbox(raw,
                                 extra_detail=self._detail())
        if inbox.status != "ok":
            return ReadResult(status=inbox.status, detail=inbox.detail)
        matches = [r for r in inbox.rows if r.label == row.label]
        if len(matches) != 1:
            return ReadResult(status="ambiguous_identity",
                              detail=f"row_label_matches_{len(matches)}")

        self._offsite = None
        previous = thread_id_from_url(self.page.url or "")
        selected = any(r.get('pressed') and r.get('lines', [None])[0] == row.label
                       for r in raw.get('rows', []))
        clicked = await self.page.evaluate(_JS_CLICK_ROW, row.label)
        if clicked != 1:
            return ReadResult(status="ambiguous_identity", detail="row_changed_before_click")
        try:
            await self.page.wait_for_function(_JS_THREAD_OPENED,
                                             arg=None if selected else previous, timeout=10_000)
        except Exception:
            return ReadResult(status="dom_unknown", navigations_used=1,
                              detail="row_did_not_open_a_thread")
        self._assert_on_site()
        if not await self._wait_readable(thread=True):
            return ReadResult(status="dom_unknown", navigations_used=1,
                              detail="thread_did_not_settle")
        raw = await self.page.evaluate(JS_RAW)
        selected_rows = [r for r in raw.get('rows', []) if r.get('pressed')]
        if (len(selected_rows) != 1 or selected_rows[0].get('lines', [None])[0] != row.label):
            return ReadResult(status="ambiguous_identity", navigations_used=1,
                              detail="opened_row_not_selected")
        failed = await self._check_account()
        if failed:
            return ReadResult(status=failed.status, navigations_used=1, detail=failed.detail)
        return interpret_read(raw,
                              thread_id_from_url(self.page.url or ""), 1)

    async def read_thread(self, thread_id: str, want: int = 0, *, anchor: tuple = ()) -> ReadResult:
        from shadow.dom import read_coverage
        return read_coverage(await self._read_thread(thread_id, want, anchor=anchor),
                             want if want > 0 else None)

    async def _read_thread(self, thread_id: str, want: int = 0, *, anchor: tuple = ()) -> ReadResult:
        assert self.page
        failed = await self._check_account()
        if failed:
            return ReadResult(status=failed.status, detail=failed.detail)
        navigations = 0
        if not same_thread(thread_id_from_url(self.page.url or ""), thread_id):
            if want > 0:
                return ReadResult(status="wrong_thread", detail="history_requires_current_thread")
            navigations = await self._goto(IG_THREAD.format(thread_id=thread_id))
            if not same_thread(thread_id_from_url(self.page.url or ""), thread_id):
                return ReadResult(status="wrong_thread", thread_id=None,
                                  navigations_used=navigations,
                                  detail="url_has_no_requested_thread")

        try:
            await self.page.wait_for_function(_JS_ON_THREAD, timeout=15_000)
        except Exception:
            return ReadResult(status="dom_unknown", thread_id=thread_id,
                              navigations_used=navigations, detail="no_thread_url")

        raw = await self.page.evaluate(JS_RAW)
        if (navigations or not raw.get("pane_present")
                or raw.get('loading') or not raw.get('articles')
                or any(not article.get("laid") or not (article.get('text') or article.get('media_label'))
                       for article in raw.get("articles", []))):
            if not await self._wait_readable(thread=True):
                return ReadResult(status="dom_unknown", thread_id=thread_id,
                                  navigations_used=navigations, detail="thread_did_not_settle")
            raw = await self.page.evaluate(JS_RAW)
        result = (await self._load_history(want, anchor=anchor) if want > 0
                  else interpret_read(raw, thread_id))
        failed = await self._check_account()
        if failed:
            return ReadResult(status=failed.status, navigations_used=navigations,
                              detail=failed.detail)
        if not same_thread(thread_id_from_url(self.page.url or ""), thread_id):
            return ReadResult(status="wrong_thread", thread_id=None,
                              navigations_used=navigations,
                              detail="thread_changed_during_read")
        return replace(result, navigations_used=navigations,
                       detail=result.detail or self._detail())

    async def _load_history(self, want: int, *, anchor: tuple = ()) -> ReadResult:
        from shadow.dom import read_coverage
        return read_coverage(await self._load_history_guarded(want, anchor=anchor), want)

    async def _load_history_guarded(self, want: int, *, anchor: tuple = ()) -> ReadResult:
        assert self.page
        initial = await self.page.evaluate(JS_RAW)
        if initial.get('composer_text'):
            return replace(interpret_read(initial, thread_id_from_url(initial.get('path'))),
                           status='dom_unknown', detail='history_manual_text')
        immediate = interpret_read(initial, thread_id_from_url(initial.get('path')))
        if any(not e.laid_out for e in immediate.events):
            return replace(immediate, status='dom_unknown', complete=False, detail='history_unlaid_messages')
        if (immediate.status == 'ok' and all(e.laid_out for e in immediate.events)
                and sum(e.kind in ('text', 'media', 'unknown') for e in immediate.events) >= want):
            return replace(immediate, detail='history_count_reached', coverage={**immediate.coverage,
                'scroll_position_restored': True, 'view_content_restored': True,
                'restoration_reason': 'no_scroll_required'})
        if not await self.page.evaluate(_JS_HISTORY_BEGIN):
            return ReadResult('dom_unknown', detail='history_guard_unavailable')
        try:
            result = await self._collect_history(want, anchor=anchor)
        finally:
            restoration = await self.page.evaluate(_JS_HISTORY_END, [JS_RAW,
                self.settings.history_step_timeout_ms, self.settings.dom_settle_ms,
                self.settings.dom_sample_ms,
                [json.dumps([a.get('text'), a.get('media_label'), a.get('reply_to_text')],
                            ensure_ascii=False, separators=(',', ':'))
                 for a in initial.get('articles', []) if a.get('laid')]])
        from shadow import alignment
        anchors = alignment.keys_of([e for e in alignment.sequence_events(interpret_read(
            initial, thread_id_from_url(initial.get('path'))).events) if e.laid_out])
        restored_events = interpret_read(restoration.get('raw', {}),
                                        thread_id_from_url(initial.get('path'))).events
        visible = alignment.keys_of([e for e in alignment.sequence_events(restored_events) if e.laid_out])
        content_restored = bool(anchors) and any(visible[i:i + len(anchors)] == anchors
                                                for i in range(len(visible) - len(anchors) + 1))
        coverage = {**result.coverage, 'requested_count': want,
                    'collected_count': sum(e.kind in ('text', 'media', 'unknown') for e in result.events),
                    'collection_stop': result.detail,
                    'budget_exhausted': 'budget_exhausted' in result.detail,
                    'ambiguous': result.detail == 'history_ambiguous',
                    'owner_interrupted': restoration['reason'] == 'owner_interrupted',
                    'stored_anchor': result.detail == 'history_stored_anchor',
                    'restored_rendered_count': len(visible),
                    'scroll_position_restored': restoration['restored'],
                    'view_content_restored': content_restored,
                    'restoration_reason': restoration['reason']}
        if result.status in ('login_required', 'challenge', 'receipts_visible', 'wrong_account', 'wrong_thread'):
            return replace(result, coverage=coverage)
        if restoration['reason'] in ('owner_interrupted', 'view_replaced'):
            return replace(result, status='dom_unknown', complete=False,
                           detail='history_' + restoration['reason'], coverage=coverage)
        if restoration['reason'] == 'receipts_visible':
            return replace(result, status='receipts_visible', coverage=coverage)
        if not restoration['restored'] or not content_restored:
            return replace(result, status='dom_unknown', complete=False,
                           detail='history_view_restore_failed', coverage=coverage)
        return replace(result, coverage=coverage)

    async def _collect_history(self, want: int, *, anchor: tuple = ()) -> ReadResult:
        """Collect bounded overlapping windows, oldest first, without navigation.

        No content hash deduplication, guessed overlap or silent unmatched page.
        Reaching a visual edge or timing out is never server-history proof.
        """
        from shadow import alignment

        raw = await self.page.evaluate(JS_RAW)
        path = raw.get('path')
        thread_id = thread_id_from_url(path)
        result = interpret_read(raw, thread_id)
        result = replace(result, events=tuple(alignment.sequence_events(result.events)))
        if any(not e.laid_out for e in result.events):
            return replace(result, status='dom_unknown', complete=False, detail='history_unlaid_messages')
        events = list(result.events)
        deadline = asyncio.get_running_loop().time() + self.settings.dom_read_timeout_ms / 1000
        for _ in range(self.settings.history_max_steps):
            if result.status != 'ok':
                return result
            if anchor and alignment.align(anchor, alignment.keys_of(events)).status == 'ok':
                return replace(result, events=tuple(events), detail='history_stored_anchor')
            if sum(e.kind in ('text', 'media', 'unknown') for e in events) >= want:
                return replace(result, events=tuple(events), detail='history_count_reached')
            remaining = int((deadline - asyncio.get_running_loop().time()) * 1000)
            if remaining <= 0:
                return replace(result, status='dom_unknown', events=tuple(events), complete=False,
                               detail='history_budget_exhausted')
            self._assert_on_site()
            settled = await self.page.evaluate(_JS_WAIT_RAW, [
                JS_RAW, True, path, min(remaining, self.settings.history_step_timeout_ms),
                self.settings.dom_settle_ms, self.settings.dom_sample_ms, True,
            ])
            self._assert_on_site()
            if settled['reason'] == 'path_changed':
                return replace(result, status='wrong_thread', events=tuple(events), complete=False,
                               detail='thread_changed_during_history')
            if settled['reason'] in ('owner_interrupted', 'pane_replaced'):
                return replace(result, status='dom_unknown', events=tuple(events), complete=False,
                               detail='history_' + settled['reason'])
            older = interpret_read(settled['raw'], thread_id)
            older = replace(older, events=tuple(alignment.sequence_events(older.events)))
            if any(not e.laid_out for e in older.events):
                return replace(result, status='dom_unknown', events=tuple(events), complete=False,
                               detail='history_unlaid_messages')
            if older.status != 'ok':
                return older
            if settled['reason'] != 'settled':
                return replace(result, status='dom_unknown', events=tuple(events), complete=False,
                               detail='history_load_unsettled')
            old_keys, new_keys = alignment.keys_of(older.events), alignment.keys_of(events)
            if old_keys == new_keys:
                if settled.get('moved') and not settled['raw'].get('at_visual_start'):
                    continue
                return replace(result, status='dom_unknown', events=tuple(events), complete=False,
                               detail='history_no_progress')
            overlap = alignment.align(old_keys, new_keys, allow_containment=False)
            if overlap.status != 'ok' or not overlap.overlap:
                return replace(result, status='dom_unknown', events=tuple(events), complete=False,
                               detail=f'history_{overlap.status}')
            added = list(older.events[:-overlap.overlap])
            if not added:
                return replace(result, status='dom_unknown', events=tuple(events), complete=False,
                               detail='history_no_progress')
            # Orders/targets describe one raw window, not a merged event ID.
            # Retain source-window relationships rather than pretending their
            # integer orders became stable when prepending history.
            def scoped(e, scope):
                return replace(e, evidence={**e.evidence, 'observation_scope': scope})
            added = [scoped(e, len(events)) for e in added]
            events = [scoped(e, 0) if 'observation_scope' not in e.evidence else e for e in events]
            events = [replace(e, position=i) for i, e in enumerate(added + events)]
            result = replace(older, events=tuple(events), complete=False)
        return replace(result, status='dom_unknown', complete=False, detail='history_step_budget_exhausted')

    async def send_one(self, thread_id: str, text: str, check: SendCheck) -> SendResult:
        assert self.page
        url = self.page.url or ""
        # The host check comes first and is never skipped for a fixture: this is
        # the guard against typing a DM into a page we did not choose.
        if not url or not host_allowed(url):
            return SendResult(status="failed_before_submit", error_code="off_site")
        if not same_thread(thread_id_from_url(url), thread_id):
            return SendResult(status="failed_before_submit", thread_id=None,
                              error_code="wrong_thread")
        failed = await self._check_account()
        if failed:
            return SendResult(status="failed_before_submit", thread_id=thread_id,
                              error_code=failed.detail)

        async def bound_check(point: CheckPoint) -> bool:
            if (self._offsite or not host_allowed(self.page.url or "")
                    or not same_thread(thread_id_from_url(self.page.url or ""), thread_id)):
                return False
            return await self._check_account() is None and await check(point)

        return await _send_on_page(self.page, thread_id, text, bound_check)

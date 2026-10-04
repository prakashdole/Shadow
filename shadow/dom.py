"""The one copy of the in-page reader, plus its pure-Python interpretation.

The JavaScript below returns RAW STRUCTURE only: geometry, roles, text and
attribute names. Every decision - what a message is, who sent it, whether a
thread is empty or merely unreadable - is made here in Python, so it is
testable without a browser and so there is exactly one parser in the tree.

No Playwright import. The rules come from tests/fixtures/dom/FINDINGS.md:

* A thread row is the outermost ``div[role=button]`` 60-90px tall inside
  ``[aria-label="Thread list"]``. One per thread: each live thread renders five
  nested divs in that band and only one of them carries ``role=button``.
* A MESSAGE is a ``div[role=article]``. Its text is the concatenated leaf
  ``[dir=auto]`` text inside it, with ``<img alt>`` substituted because
  Instagram renders emoji as images and ``innerText`` omits their alt text.
* A leaf OUTSIDE every article is never a message. It is a reaction, a day
  separator, a "replied to you" line, or a system notice.
* Nothing is de-duplicated. Two identical "ok" messages are two events (D32).
* There are no stable ids anywhere (D31). ``role=article`` order plus geometry
  is all the evidence a window carries.
"""

from __future__ import annotations

import re
from dataclasses import replace
import unicodedata

from shadow.contracts import (
    InboxResult, InboxRow, ObservedEvent, ReadResult, ReadStatus,
)

# --- tunables (settings live in shadow/settings.py; these are structural) ---

# PROPOSED SETTING: row_height_min_px / row_height_max_px - a thread row band.
ROW_MIN_PX = 60
ROW_MAX_PX = 90
# PROPOSED SETTING: pane_min_height_px - ignore small scroll boxes as panes.
PANE_MIN_PX = 200
# PROPOSED SETTING: separator_min_width_ratio - a full-pane leaf is a separator.
SEPARATOR_WIDTH_RATIO = 0.9

COMPOSER_SELECTOR = 'div[contenteditable="true"][aria-label="Message"]'

_LOGIN_PATHS = ("/accounts/login",)
_CHALLENGE_PATHS = ("/challenge", "/checkpoint", "/suspended", "/accounts/suspended")

# --- text patterns ------------------------------------------------------

# Both day-separator formats observed live. The comma format matched no
# production noise rule and survived only on width, which is why width alone is
# not enough and both are matched by text too.
_SEP_TEXT = re.compile(
    r"^(?:"
    r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+\d{1,2}:\d{2}"
    r"|\d{1,2}\s+[a-z]{3,9},?\s+\d{1,2}:\d{2}"
    r"|[a-z]{3,9}\s+\d{1,2},?\s+\d{1,2}:\d{2}"
    r"|today|yesterday|earlier|new messages"
    r"|\d{1,2}:\d{2}"
    r")(?:\s*[ap]m)?$",
    re.I,
)
_REACTION_TEXT = re.compile(r"\breacted\b|\bliked a message\b|\byou reacted\b", re.I)
_REPLY_TO_TEXT = re.compile(
    r"replied to (?:you|yourself)\b|^you replied to\b|\breplied to @", re.I
)
_SEEN_TEXT = re.compile(r"^seen(?: by .*| just now| \d+\s*[smhdw](?: ago)?)?$", re.I)
_UNAVAILABLE = re.compile(r"^(reel|post|story) unavailable$", re.I)
_AGE_TAIL = re.compile(r"(?:\s*·\s*|\s+)\d+[smhdw]\s*$", re.I)
_UNREAD_TOKEN = re.compile(r"(^|\s)unread(\s|$)", re.I)
_DELIVERY_NOTICE = re.compile(r"^this account can.?t receive your message\b", re.I)

_OWNER_LAST = re.compile(r"^you:", re.I)
_OWNER_SENT = re.compile(r"^you sent\b", re.I)
_REACTED_TO_OWNER = re.compile(r"reacted(?:\s[^ ]{1,8})?\s+to your message", re.I)
_ATTACHMENT_IN = re.compile(
    r"sent an attachment|sent a (?:reel|post|photo|video)|\bshared\b", re.I
)

_THREAD_PATH = re.compile(r"/direct/t/(\d+)(?:/|$|[?#])")


# --- the single copy of the in-page JavaScript --------------------------

# Online badges live outside the dir=auto name/preview leaves. The click and
# inbox reader must use the same label extraction, including fixture fallback.
JS_ROW_LINES = r"""const rowLines = el => {
    const leaves = [...el.querySelectorAll('[dir="auto"]')]
        .filter(n => !n.querySelector('[dir="auto"]'));
    if (leaves.length) return leaves.map(n => (n.innerText || '').trim()).filter(Boolean);
    return (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
};"""

# Prefer a message ancestor: scanning every div is only a cold-layout fallback.
# The same discovery is used by the reader and history loader.
JS_FIND_PANE = r"""const findMessagePane = () => {
    const list = document.querySelector('[aria-label="Thread list"]');
    const valid = el => {
        if (el.clientHeight < 200 || (list && list.contains(el))) return false;
        if (!/^(auto|scroll)$/.test(getComputedStyle(el).overflowY)) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.left >= document.documentElement.clientWidth * 0.15;
    };
    const first = document.querySelector('[role="article"]');
    for (let el = first?.parentElement; el; el = el.parentElement) {
        if (valid(el)) return el;
    }
    for (const el of document.querySelectorAll('[role="log"], [aria-label="Messages"]')) {
        if (valid(el)) return el;
    }
    let pane = null, width = 0;
    for (const el of document.querySelectorAll('div')) {
        if (!valid(el)) continue;
        const w = el.getBoundingClientRect().width;
        if (w > width) { pane = el; width = w; }
    }
    return pane;
};"""

JS_MESSAGE_ROOTS = r"""const hiddenTree = (root, pane) => {
    for (let node = root; node && node !== pane; node = node.parentElement) {
        if (node.hidden || node.getAttribute('aria-hidden') === 'true'
                || getComputedStyle(node).display === 'none') return true;
    }
    return false;
};
const messageRoots = pane => {
    const articles = [...pane.querySelectorAll('[role="article"]')]
        .filter(el => !el.parentElement?.closest('[role="article"]')
            && !hiddenTree(el, pane));
    const rows = [...pane.querySelectorAll('[role="row"][aria-roledescription="message"]')]
        .filter(el => !hiddenTree(el, pane));
    // Never union competing profiles or interpret ordinary table/inbox rows.
    if (articles.length) return {profile: rows.length ? 'mixed_unknown' : 'articles',
                                  roots: rows.length ? [] : articles};
    if (pane.matches('[role="log"][aria-label="Messages"]') && rows.length
            && rows.every(el => !el.parentElement?.closest('[role="row"]')
                && el.querySelector('[dir="auto"], [aria-labelledby]'))) {
        return {profile: 'semantic_rows', roots: rows};
    }
    return {profile: 'unknown', roots: []};
};"""

# One evaluate returns everything both readers need. `window.__fixturePath` is
# only ever set by a test on a file:// page; on instagram.com it is undefined
# and location.pathname is used.
JS_RAW = r"""() => {
""" + JS_ROW_LINES + JS_FIND_PANE + JS_MESSAGE_ROOTS + r"""
    const OUT = {
        path: (window.__fixturePath !== undefined && window.__fixturePath !== null)
            ? String(window.__fixturePath)
            : String(location.pathname || ''),
        list_present: false,
        inbox_empty_verified: false,
        rows: [],
        pane_present: false,
        pane: null,
        at_start: false,
        loading: false,
        header: {profile_links: 0, avatars: 0},
        articles: [],
        outside: [],
        composer_text: '',
        seen: false,
    };

    // innerText skips <img alt>, so an emoji-only message reads as "".
    const textOf = el => {
        let out = '';
        const walk = n => {
            if (n.nodeType === 3) { out += n.nodeValue; return; }
            if (n.nodeType !== 1) return;
            const tag = n.tagName;
            if (tag === 'IMG') { const a = n.getAttribute('alt'); if (a) out += a; return; }
            if (tag === 'BR') { out += '\n'; return; }
            for (const c of n.childNodes) walk(c);
        };
        walk(el);
        return out.replace(/\s*\n\s*/g, ' ').trim();
    };
    const leavesOf = root => [...root.querySelectorAll('[dir="auto"]')]
        .filter(el => el.querySelector('[dir="auto"]') === null);
    const quoteNodes = new Set();
    const rect = el => {
        const r = el.getBoundingClientRect();
        const laid = r.width > 0 && r.height > 0;
        return {
            left: Math.round(r.left * 100) / 100,
            width: Math.round(r.width * 100) / 100,
            centre: Math.round((r.left + r.width / 2) * 100) / 100,
            laid: laid,
        };
    };

    // --- thread list -------------------------------------------------
    const list = document.querySelector('[aria-label="Thread list"]');
    OUT.list_present = !!list;
    // A rendered but still-populating list is not an empty inbox. No current
    // live empty-state marker has been verified; static fictional files only.
    OUT.inbox_empty_verified = !!list && location.protocol === 'file:';
    if (list) {
        const band = [...list.querySelectorAll('div[role="button"]')].filter(el => {
            const h = el.getBoundingClientRect().height;
            return h >= 60 && h <= 90;
        });
        // One row per thread: keep only the outermost, drop nested role=buttons.
        const rows = band.filter(el => !band.some(other => other !== el && el.contains(other)));
        for (const el of rows) {
            const lines = rowLines(el);
            if (!lines.length) continue;
            const g = rect(el);
            OUT.rows.push({
                top: Math.round(el.getBoundingClientRect().top),
                height: Math.round(el.getBoundingClientRect().height),
                lines: lines,
                full_text: (el.innerText || '').trim(),
                pressed: el.getAttribute('aria-pressed') === 'true',
                width: g.width,
            });
        }
        OUT.rows.sort((a, b) => a.top - b.top);
    }

    // --- message pane: message ancestor, then cold-layout fallback -------
    const vw = document.documentElement.clientWidth;
    const pane = findMessagePane();
    OUT.pane_present = !!pane;
    if (pane) {
        const profile = messageRoots(pane);
        OUT.parser_profile = profile.profile;
        const roots = new Set(profile.roots);
        const rootSelector = profile.profile === 'semantic_rows' ? '[role="row"]' : '[role="article"]';
        const inMessage = el => !!el.closest(rootSelector);
        OUT.geometry_sender_verified = profile.profile === 'articles'
            && getComputedStyle(pane).direction === 'ltr';
        const pr = pane.getBoundingClientRect();
        OUT.pane = {left: pr.left, right: pr.right, width: pr.width};
        // Instagram's column-reverse starts at zero at the NEWEST message.
        // Its virtualized upper edge is not evidence of full history either.
        const reversed = getComputedStyle(pane).flexDirection === 'column-reverse';
        const extent = Math.max(0, pane.scrollHeight - pane.clientHeight);
        OUT.at_visual_start = reversed ? pane.scrollTop <= -extent + 2 : pane.scrollTop <= 2;
        // A static fictional file has no server-side pages. Real Instagram's
        // visual edge never proves server history completeness.
        OUT.at_start = location.protocol === 'file:' && OUT.at_visual_start;
        OUT.loading = pane.getAttribute('aria-busy') === 'true'
            || [...pane.querySelectorAll('[role="progressbar"]')].some(el => {
                if (el.closest('[role="article"]')) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && r.top >= pr.top - 2
                    && r.bottom <= pr.top + 80;
            });

        // Header evidence: the strip above the message list, right of the list.
        for (const a of document.querySelectorAll('a[href]')) {
            const r = a.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            if (pane.contains(a) || (list && list.contains(a))) continue;
            if (r.left < vw * 0.15) continue;
            if (r.top > pr.top + 140) continue;
            const href = a.getAttribute('href') || '';
            if (!/^\/[^/]+\/?$/.test(href)) continue;     // a profile link shape
            OUT.header.profile_links++;
            if (a.querySelector('img')) OUT.header.avatars++;
        }

        // Media: a card is a role=button wrapper, or a direct media link.
        const mediaOf = art => {
            for (const card of art.querySelectorAll('[role="presentation"]')) {
                const parts = leavesOf(card).map(textOf).filter(Boolean);
                if (parts.length === 2 && parts[0] === 'Message unavailable'
                        && parts[1] === 'This content may have been deleted by its owner or hidden by their privacy settings.') {
                    return 'message_unavailable';
                }
            }
            for (const icon of art.querySelectorAll('svg[aria-label], svg title')) {
                const label = icon.getAttribute('aria-label') || icon.textContent || '';
                if (/^(Clip|Reel)$/i.test(label.trim())) return 'reel';
                if (/^(Post|Collage)$/i.test(label.trim())) return 'post';
            }
            const direct = art.querySelector('a[href*="/reel/"], a[href*="/tv/"], a[href*="/p/"]');
            if (direct) {
                const h = direct.getAttribute('href') || '';
                if (h.indexOf('/reel/') !== -1) return 'reel';
                if (h.indexOf('/tv/') !== -1) return 'video';
                return 'post';
            }
            for (const b of art.querySelectorAll('[role="button"]')) {
                const txt = (b.innerText || '').replace(/\s+/g, ' ').trim();
                const m = txt.match(/^(.{0,40}?)(Clip|Post|Reel)$/i);
                if (m) return m[2].toLowerCase() === 'post' ? 'post' : 'reel';
            }
            // Some live shares expose neither a type icon nor a permalink.
            // A creator profile plus a large image card proves media, not type.
            const creator = [...art.querySelectorAll('a[href]')]
                .some(a => /^\/[^/]+\/?$/.test(a.getAttribute('href') || ''));
            if (creator && [...art.querySelectorAll('[role="button"] img')].some(img => {
                const r = img.getBoundingClientRect();
                return r.width >= 100 && r.height >= 100;
            })) return 'shared_media';
            return null;
        };

        for (const art of profile.roots) {
            const g = rect(art);
            // A quoted reply target is rendered as its own leaf; it is evidence
            // about the message, not the message text.
            let replyTo = null;
            for (const q of art.querySelectorAll('blockquote, [role="blockquote"]')) {
                const t = leavesOf(q).map(textOf).filter(Boolean).join(' ');
                if (t) { replyTo = t; break; }
            }
            // Live replies render the quoted card before the article, not
            // inside a blockquote. Bind only the adjacent quote beneath an
            // explicit reply marker, never a previous ordinary message.
            for (let node = art; !replyTo && node && node !== pane; node = node.parentElement) {
                const quote = node.previousElementSibling;
                if (!quote || quote.querySelector(rootSelector)) continue;
                const marker = quote.previousElementSibling;
                if (!marker || marker.querySelector(rootSelector)) continue;
                if (hiddenTree(quote, pane) || hiddenTree(marker, pane)) continue;
                const markerText = (marker.innerText || '').replace(/\s+/g, ' ').trim();
                if (!/^(?:.{1,100} replied to (?:you|yourself)|you replied to .{1,100})(?: · \d+[smhdw])?$/i.test(markerText)) continue;
                const label = mediaOf(quote);
                const text = leavesOf(quote).map(textOf).filter(Boolean).join(' ');
                if (/reacted to (?:your|a) message|^this message was unsent$/i.test(text)) continue;
                replyTo = label ? '[quoted ' + label + ']' : text || null;
                if (replyTo) quoteNodes.add(quote);
            }
            const own = leavesOf(art)
                .filter(el => !el.closest('blockquote, [role="blockquote"]'))
                .map(textOf).filter(Boolean);
            // Large emoji-only bubbles have no dir=auto. Their labelled
            // role=none content is still text, not an unsupported empty card.
            if (!own.length) {
                const labelled = document.getElementById(art.getAttribute('aria-labelledby'));
                if (labelled && art.contains(labelled) && labelled.getAttribute('role') === 'none') {
                    const text = textOf(labelled);
                    if (text) own.push(text);
                }
            }
            OUT.articles.push({
                left: g.left, width: g.width, centre: g.centre, laid: g.laid,
                top: Math.round(art.getBoundingClientRect().top),
                text: own.join(' '),
                media_label: mediaOf(art),
                reply_to_text: replyTo,
                // Exact separate UI marker, never a word in message/quote text.
                edited: [...art.querySelectorAll('span, [role="status"]')].some(el =>
                    !el.closest('[dir="auto"], blockquote, [role="blockquote"]')
                    && !el.children.length && (el.innerText || '').trim() === 'Edited'),
            });
        }

        // Leaves outside every article, in document order, interleaved with the
        // articles by `order` so a reply-target line can attach to the next one.
        let order = 0;
        const items = [];
        for (const el of pane.querySelectorAll('[role="article"], [role="row"], [dir="auto"], [role="button"]')) {
            if (hiddenTree(el, pane)) continue;
            if (roots.has(el)) { items.push({order: order++, article: true, node: el}); continue; }
            if (inMessage(el)) continue;
            if ([...quoteNodes].some(q => q.contains(el))) continue;
            if (el.getAttribute('role') === 'button') {
                if (el.querySelector('[dir="auto"], [role="article"], [role="button"]') || el.closest('[dir="auto"]')) continue;
                const text = textOf(el);
                if (!/\p{Extended_Pictographic}/u.test(text)
                        || !/^[\p{Extended_Pictographic}\p{Emoji_Presentation}\p{Emoji_Modifier}\uFE0F\u200D\s\d]+$/u.test(text)) continue;
                let target = null;
                for (let node = el.parentElement; node && node !== pane; node = node.parentElement) {
                    const articles = profile.roots.filter(root => node.contains(root));
                    if (articles.length > 1) break;
                    if (articles.length === 1) {
                        if (articles[0].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) target = articles[0];
                        break;
                    }
                }
                if (!target) continue;
                const g = rect(el);
                items.push({order: order++, article: false, ...g, text: text,
                            reaction: true, target: target});
                continue;
            }
            if (el.querySelector('[dir="auto"]') !== null) continue;   // not a leaf
            if (el.closest('[role="article"]') !== null) continue;     // inside a message
            const g = rect(el);
            items.push({
                order: order++, article: false,
                left: g.left, width: g.width, centre: g.centre, laid: g.laid,
                text: textOf(el),
            });
        }
        OUT.outside = items.filter(i => !i.article).map(i => ({
            order: i.order, left: i.left, width: i.width,
            centre: i.centre, laid: i.laid, text: i.text,
            ...(i.reaction ? {reaction: true,
                target_order: items.find(a => a.article && a.node === i.target)?.order} : {}),
        }));
        OUT.article_orders = items.filter(i => i.article).map(i => i.order);
        // A receipt is visible thread chrome, not a typed message, quoted
        // message or inbox preview that happens to say "Seen".
        OUT.seen = OUT.outside.some(i => i.laid && !i.reaction
            && /^seen(?: by .*| just now| \d+\s*[smhdw](?: ago)?)?$/i.test(i.text));
    }

    const box = document.querySelector('div[contenteditable="true"][aria-label="Message"]');
    OUT.composer_text = box ? (box.innerText || '').trim() : '';

    return OUT;
}"""

# The composer, for the send primitive. Kept beside the reader so the two agree
# on what "the composer" is.
JS_COMPOSER = r"""() => {
    const box = document.querySelector('div[contenteditable="true"][aria-label="Message"]');
    if (!box) return null;
    box.focus();
    return (box.innerText || '').trim();
}"""

# Clear whatever is in the composer. Used only when the coordinator aborts
# between typing and Enter: the text must not survive, or a later send could
# send it. Reports whether the box is now empty.
JS_CLEAR_COMPOSER = r"""() => {
    const box = document.querySelector('div[contenteditable="true"][aria-label="Message"]');
    if (!box) return null;
    box.focus();
    const r = document.createRange();
    r.selectNodeContents(box);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    return true;
}"""


# --- pure interpretation -----------------------------------------------

def normalize(text: str) -> str:
    """NFC, whitespace collapsed, stripped. Never case-folded: case is content."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or "")).strip()


def thread_id_from_url(url: str) -> str | None:
    """Digits from /direct/t/<digits>/ only. Comparison is always ==, never in."""
    m = _THREAD_PATH.search(url or "")
    return m.group(1) if m else None


def same_thread(left: str | None, right: str | None) -> bool:
    """Exact equality. '12' is not '123' and neither contains the other."""
    return bool(left) and left == right


def strip_age(text: str) -> str:
    """Remove the trailing age token and the bare '·' separator from a preview."""
    t = re.sub(r"\s+Unread$", "", normalize(text), flags=re.I)
    t = _AGE_TAIL.sub("", t)
    return t.rstrip("·").strip()


def page_status(path: str) -> ReadStatus | None:
    p = (path or "").lower()
    if any(x in p for x in _CHALLENGE_PATHS):
        return "challenge"
    if any(x in p for x in _LOGIN_PATHS):
        return "login_required"
    return None


def classify_preview(preview: str) -> str:
    """owner_last | reaction_to_owner | attachment_in | text_in | unknown."""
    t = normalize(preview)
    if not t:
        return "unknown"
    if _DELIVERY_NOTICE.match(t):
        return "unknown"
    if _OWNER_LAST.match(t) or _OWNER_SENT.match(t):
        return "owner_last"
    if _REACTED_TO_OWNER.search(t):
        return "reaction_to_owner"
    if _ATTACHMENT_IN.search(t):
        return "attachment_in"
    return "text_in"


def interpret_rows(raw: dict) -> tuple[InboxRow, ...]:
    """Raw row dicts -> InboxRow. Presentation only; never an identity."""
    rows = raw.get("rows") or []
    labels = [r["lines"][0] for r in rows if r.get("lines")]
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1

    out = []
    for index, row in enumerate(rows):
        lines = row.get("lines") or []
        if not lines:
            continue
        label = lines[0]
        preview = strip_age(" ".join(lines[1:]))
        cls = classify_preview(preview)
        explicit_unread = bool(_UNREAD_TOKEN.search(row.get("full_text") or ""))
        out.append(
            InboxRow(
                row_index=index,
                label=label,
                preview_text=preview,
                preview_class=cls,          # type: ignore[arg-type]
                # Instagram never renders the word; this is inferred, not read.
                unread_hint=explicit_unread or cls in ("text_in", "attachment_in"),
                label_is_unique=counts.get(label, 0) == 1,
                # P00b Q7: a group row is byte-identical to a 1:1 row.
                group_hint=None,
            )
        )
    return tuple(out)


def inbox_status(raw: dict) -> tuple[ReadStatus, str]:
    status = page_status(raw.get("path", ""))
    if status:
        return status, "page_path"
    if not raw.get("list_present"):
        return "dom_unknown", "thread_list_missing"
    if not raw.get("rows"):
        if raw.get('inbox_empty_verified') is False:
            return 'dom_unknown', 'inbox_rows_not_ready'
        return "empty_verified", "thread_list_rendered_no_rows"
    if raw.get("seen"):
        return "receipts_visible", "seen_leaf_in_thread"
    return "ok", ""


def interpret_inbox(raw: dict, navigations_used: int = 0,
                    extra_detail: str = "") -> InboxResult:
    status, detail = inbox_status(raw)
    return InboxResult(
        status=status,
        rows=interpret_rows(raw) if status == "ok" else (),
        navigations_used=navigations_used,
        detail=detail or extra_detail,
    )


def _outside_kind(text: str, laid: bool, width_ratio: float) -> tuple[str | None, str | None]:
    """(action, kind) for a leaf outside every article.

    action is "reaction" (becomes an event), "drop" (separator), "reply_to"
    (attaches to the next article) or "system" (becomes an event).
    """
    t = normalize(text)
    if not t:
        return "drop", None
    if _REACTION_TEXT.search(t):
        return "reaction", "reaction"
    # Width alone was the old rule and it was wrong: a separator rendered
    # narrower than 90% of the pane came back as a message. Match the text too,
    # for both formats seen live.
    if (laid and width_ratio >= SEPARATOR_WIDTH_RATIO) or _SEP_TEXT.match(t):
        return "drop", None
    if _REPLY_TO_TEXT.search(t):
        return "reply_to", None
    return "system", "system"


def _outside_action(leaf, pane_w):
    if leaf.get('reaction'):
        return 'reaction', 'reaction'
    return _outside_kind(leaf['text'], leaf['laid'], (leaf['width'] / pane_w) if pane_w else 0.0)


def read_coverage(result: ReadResult, requested_count: int | None = None) -> ReadResult:
    """Normalize facts on every return, including safety holds and early exits."""
    count = sum(e.kind in ('text', 'media', 'unknown') for e in result.events)
    coverage = {
        'parser_profile': 'unknown', 'visual_edge': False, 'server_complete': False,
        'requested_count': requested_count, 'collected_count': count,
        'rendered_count': sum(e.laid_out and e.kind in ('text', 'media', 'unknown') for e in result.events),
        'collection_stop': result.detail, 'budget_exhausted': 'budget_exhausted' in result.detail,
        'ambiguous': result.detail == 'history_ambiguous',
        'owner_interrupted': result.detail == 'history_owner_interrupted',
        'stored_anchor': result.detail == 'history_stored_anchor',
        'scroll_position_restored': None, 'view_content_restored': None,
        'restoration_reason': 'not_attempted', 'restored_rendered_count': 0,
        **result.coverage,
    }
    # Counts describe this returned window, not a previous collection step.
    coverage.update(requested_count=requested_count, collected_count=count,
                    requested_count_reached=requested_count is not None and count >= requested_count,
                    collection_stop=result.detail,
                    budget_exhausted=coverage['budget_exhausted'] or 'budget_exhausted' in result.detail,
                    ambiguous=coverage['ambiguous'] or result.detail == 'history_ambiguous',
                    stored_anchor=coverage['stored_anchor'] or result.detail == 'history_stored_anchor',
                    owner_interrupted=coverage['owner_interrupted'] or result.detail == 'history_owner_interrupted',
                    server_complete=False)
    coverage['collection_interrupted'] = coverage['owner_interrupted']
    return replace(result, coverage=coverage)


def interpret_read(
    raw: dict, thread_id: str | None, navigations_used: int = 0,
    extra_detail: str = '',
) -> ReadResult:
    return read_coverage(_interpret_read(raw, thread_id, navigations_used, extra_detail))


def _interpret_read(
    raw: dict, thread_id: str | None, navigations_used: int = 0,
    extra_detail: str = "",
) -> ReadResult:
    status = page_status(raw.get("path", ""))
    if status:
        return ReadResult(status=status, navigations_used=navigations_used,
                          detail="page_path")
    if not raw.get("pane_present"):
        return ReadResult(status="dom_unknown", navigations_used=navigations_used,
                          detail="message_pane_missing")
    if raw.get("seen"):
        return ReadResult(status="receipts_visible", thread_id=thread_id,
                          navigations_used=navigations_used, detail="seen_leaf_in_thread")
    if raw.get("loading"):
        return ReadResult(status="dom_unknown", thread_id=thread_id,
                          navigations_used=navigations_used, detail="thread_loading")

    pane = raw.get("pane") or {}
    pane_left = pane.get("left", 0.0)
    pane_right = pane.get("right", 0.0)
    pane_mid = (pane_left + pane_right) / 2
    pane_w = pane.get("width") or 0.0

    header = raw.get("header") or {}
    links = int(header.get("profile_links") or 0)
    avatars = int(header.get("avatars") or 0)
    if links >= 3 or avatars >= 2:
        is_group: bool | None = True
    elif 1 <= links <= 2 and avatars == 1:
        is_group = False
    else:
        is_group = None

    articles = raw.get("articles") or []
    if not articles:
        return ReadResult(status="dom_unknown", thread_id=thread_id,
                          navigations_used=navigations_used, detail="no_messages_rendered")
    outside = {o["order"]: o for o in (raw.get("outside") or [])}
    orders = raw.get("article_orders") or []

    events: list[ObservedEvent] = []
    pending_reply_to: str | None = None
    for position, order in enumerate(orders):
        art = articles[position] if position < len(articles) else None
        if art is None:
            continue
        # Anything classified between the previous article and this one.
        prev = orders[position - 1] + 1 if position else 0
        for gap_order in range(prev, order):
            leaf = outside.get(gap_order)
            if not leaf:
                continue
            action, kind = _outside_action(leaf, pane_w)
            text = normalize(leaf["text"])
            if action == "reply_to":
                pending_reply_to = text
            elif action == "reaction" and text:
                events.append(ObservedEvent(
                    position=len(events), sender="unknown", kind="reaction",
                    text=text, laid_out=bool(leaf["laid"]),
                    evidence={"source": "outside_article", "order": gap_order,
                              **({"target_order": leaf['target_order']} if 'target_order' in leaf else {})},
                ))
            elif action == "system" and text:
                events.append(ObservedEvent(
                    position=len(events), sender="unknown", kind="system",
                    text=text, laid_out=bool(leaf["laid"]),
                    evidence={"source": "outside_article", "order": gap_order},
                ))

        text = normalize(art.get("text") or "")
        media = art.get("media_label")
        unavailable = _UNAVAILABLE.match(text)
        if not media and unavailable:
            media = f"{unavailable.group(1).lower()}_unavailable"
        if media:
            kind = "media"
        elif text:
            kind = "text"
        else:
            kind = "unknown"

        laid = bool(art.get("laid"))
        sender = "unknown"
        if laid and raw.get('geometry_sender_verified', raw.get('parser_profile') is None):
            sender = "owner" if float(art.get("centre", 0.0)) > pane_mid else "contact"

        evidence = {"order": order, "in_article": True}
        if art.get("edited"):
            evidence["edited"] = True
        events.append(ObservedEvent(
            position=len(events),
            sender=sender,                       # type: ignore[arg-type]
            kind=kind,                            # type: ignore[arg-type]
            text=text,
            media_label=media,
            reply_to_text=art.get("reply_to_text") or pending_reply_to,
            laid_out=laid,
            evidence=evidence,
        ))
        pending_reply_to = None

    # Trailing leaves after the last article.
    for gap_order in range(orders[-1] + 1 if orders else 0,
                           (max(outside) + 1) if outside else 0):
        leaf = outside.get(gap_order)
        if not leaf:
            continue
        action, kind = _outside_action(leaf, pane_w)
        text = normalize(leaf["text"])
        if action == "reaction" and text:
            events.append(ObservedEvent(
                position=len(events), sender="unknown", kind="reaction",
                text=text, laid_out=bool(leaf["laid"]),
                evidence={"source": "outside_article", "order": gap_order,
                          **({"target_order": leaf['target_order']} if 'target_order' in leaf else {})},
            ))
        elif action == "system" and text:
            events.append(ObservedEvent(
                position=len(events), sender="unknown", kind="system",
                text=text, laid_out=bool(leaf["laid"]),
                evidence={"source": "outside_article", "order": gap_order},
            ))

    # Reobserved bound badges stay with their raw-window target. The badge
    # event remains available to diagnostics; sequence matching ignores it.
    badges: dict[int, list[str]] = {}
    for event in events:
        target = event.evidence.get('target_order')
        if event.kind == 'reaction' and type(target) is int:
            badges.setdefault(target, []).append(event.text)
    events = [replace(event, evidence={**event.evidence,
              'reactions': badges.get(event.evidence.get('order'), [])})
              if event.evidence.get('in_article') else event for event in events]

    return ReadResult(
        status="ok",
        thread_id=thread_id,
        events=tuple(events),
        # Real Instagram DOM has no verified server-history end signal.
        complete=bool(raw.get("at_start")),
        is_group=is_group,
        participant_links=links,
        composer_text=normalize(raw.get("composer_text") or ""),
        navigations_used=navigations_used,
        detail=extra_detail,
        coverage={"parser_profile": raw.get("parser_profile", "unknown"),
                  "visual_edge": bool(raw.get("at_visual_start")),
                  "rendered_count": sum(e.laid_out and e.kind in ("text", "media", "unknown") for e in events),
                  "server_complete": False},
    )

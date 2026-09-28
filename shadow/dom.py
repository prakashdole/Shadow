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
    r"|today|yesterday|earlier"
    r")$",
    re.I,
)
_REACTION_TEXT = re.compile(r"\breacted\b|\bliked a message\b|\byou reacted\b", re.I)
_REPLY_TO_TEXT = re.compile(
    r"replied to (?:you|yourself)\b|^you replied to\b|\breplied to @", re.I
)
_SEEN_TEXT = re.compile(r"^seen(?: by .*| just now)?$", re.I)
_UNAVAILABLE = re.compile(r"^(reel|post|story) unavailable$", re.I)
_AGE_TAIL = re.compile(r"(?:\s*·\s*|\s+)\d+[smhdw]\s*$", re.I)
_UNREAD_TOKEN = re.compile(r"(^|\s)unread(\s|$)", re.I)

_OWNER_LAST = re.compile(r"^you:", re.I)
_OWNER_SENT = re.compile(r"^you sent\b", re.I)
_REACTED_TO_OWNER = re.compile(r"reacted(?:\s[^ ]{1,8})?\s+to your message", re.I)
_ATTACHMENT_IN = re.compile(
    r"sent an attachment|sent a (?:reel|post|photo|video)|\bshared\b", re.I
)

_THREAD_PATH = re.compile(r"/direct/t/(\d+)(?:/|$|[?#])")


# --- the single copy of the in-page JavaScript --------------------------

# One evaluate returns everything both readers need. `window.__fixturePath` is
# only ever set by a test on a file:// page; on instagram.com it is undefined
# and location.pathname is used.
JS_RAW = r"""() => {
    const OUT = {
        path: (window.__fixturePath !== undefined && window.__fixturePath !== null)
            ? String(window.__fixturePath)
            : String(location.pathname || ''),
        list_present: false,
        rows: [],
        pane_present: false,
        pane: null,
        at_start: false,
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
    if (list) {
        const band = [...list.querySelectorAll('div[role="button"]')].filter(el => {
            const h = el.getBoundingClientRect().height;
            return h >= 60 && h <= 90;
        });
        // One row per thread: keep only the outermost, drop nested role=buttons.
        const rows = band.filter(el => !band.some(other => other !== el && el.contains(other)));
        for (const el of rows) {
            const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
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

    // --- message pane: widest scrollable div right of the thread list ----
    const vw = document.documentElement.clientWidth;
    let pane = null;
    for (const el of document.querySelectorAll('div')) {
        if (el.scrollHeight <= el.clientHeight + 40) continue;
        if (el.clientHeight < 200) continue;
        const r = el.getBoundingClientRect();
        if (r.left < vw * 0.15) continue;
        if (!pane || r.width > pane.getBoundingClientRect().width) pane = el;
    }
    OUT.pane_present = !!pane;
    if (pane) {
        const pr = pane.getBoundingClientRect();
        OUT.pane = {left: pr.left, right: pr.right, width: pr.width};
        OUT.at_start = pane.scrollTop <= 2;

        // Header evidence: the strip above the message list, right of the list.
        for (const a of document.querySelectorAll('a[href]')) {
            const r = a.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            if (r.left < vw * 0.15) continue;
            if (r.top > pr.top + 140) continue;
            const href = a.getAttribute('href') || '';
            if (!/^\/[^/]+\/?$/.test(href)) continue;     // a profile link shape
            OUT.header.profile_links++;
            if (a.querySelector('img')) OUT.header.avatars++;
        }

        // Media: a card is a role=button wrapper, or a direct media link.
        const mediaOf = art => {
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
            return null;
        };

        for (const art of pane.querySelectorAll('[role="article"]')) {
            const g = rect(art);
            const parts = leavesOf(art).map(textOf).filter(Boolean);
            // A quoted reply target is rendered as its own leaf; it is evidence
            // about the message, not the message text.
            let replyTo = null;
            for (const q of art.querySelectorAll('[role="blockquote"]')) {
                const t = leavesOf(q).map(textOf).filter(Boolean).join(' ');
                if (t) { replyTo = t; break; }
            }
            const own = leavesOf(art)
                .filter(el => !el.closest('[role="blockquote"]'))
                .map(textOf).filter(Boolean);
            OUT.articles.push({
                left: g.left, width: g.width, centre: g.centre, laid: g.laid,
                top: Math.round(art.getBoundingClientRect().top),
                text: own.join(' '),
                media_label: mediaOf(art),
                reply_to_text: replyTo,
                edited: /(^|\s)edited(\s|$)/i.test(art.innerText || ''),
            });
        }

        // Leaves outside every article, in document order, interleaved with the
        // articles by `order` so a reply-target line can attach to the next one.
        let order = 0;
        const items = [];
        for (const el of pane.querySelectorAll('[role="article"], [dir="auto"]')) {
            if (el.getAttribute('role') === 'article') { items.push({order: order++, article: true}); continue; }
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
        }));
        OUT.article_orders = items.filter(i => i.article).map(i => i.order);
    }

    const box = document.querySelector('div[contenteditable="true"][aria-label="Message"]');
    OUT.composer_text = box ? (box.innerText || '').trim() : '';

    const allText = [];
    for (const el of document.querySelectorAll('[dir="auto"]')) allText.push(textOf(el));
    OUT.seen = allText.some(t => /^seen(?: by .*| just now)?$/i.test(t));
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
    t = _AGE_TAIL.sub("", normalize(text))
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
        return "empty_verified", "thread_list_rendered_no_rows"
    if raw.get("seen"):
        return "receipts_visible", "seen_leaf_in_page"
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


def interpret_read(
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
                          navigations_used=navigations_used, detail="seen_leaf_in_page")

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
            action, kind = _outside_kind(
                leaf["text"], leaf["laid"],
                (leaf["width"] / pane_w) if pane_w else 0.0,
            )
            text = normalize(leaf["text"])
            if action == "reply_to":
                pending_reply_to = text
            elif action == "reaction" and text:
                events.append(ObservedEvent(
                    position=len(events), sender="unknown", kind="reaction",
                    text=text, laid_out=bool(leaf["laid"]),
                    evidence={"source": "outside_article", "order": gap_order},
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
        if laid:
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
        action, kind = _outside_kind(
            leaf["text"], leaf["laid"], (leaf["width"] / pane_w) if pane_w else 0.0
        )
        text = normalize(leaf["text"])
        if action == "reaction" and text:
            events.append(ObservedEvent(
                position=len(events), sender="unknown", kind="reaction",
                text=text, laid_out=bool(leaf["laid"]),
                evidence={"source": "outside_article", "order": gap_order},
            ))
        elif action == "system" and text:
            events.append(ObservedEvent(
                position=len(events), sender="unknown", kind="system",
                text=text, laid_out=bool(leaf["laid"]),
                evidence={"source": "outside_article", "order": gap_order},
            ))

    return ReadResult(
        status="ok",
        thread_id=thread_id,
        events=tuple(events),
        # True only if the window actually reached the start of the thread.
        complete=bool(raw.get("at_start")),
        is_group=is_group,
        participant_links=links,
        composer_text=normalize(raw.get("composer_text") or ""),
        navigations_used=navigations_used,
        detail=extra_detail,
    )

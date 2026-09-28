#!/usr/bin/env python3
"""Read-only live DOM discovery for the dedicated Instagram profile.

Connects to the owner's Chrome for Testing profile over CDP and READS the
rendered DOM. It never sends, types, presses, clicks, scrolls, focuses or
dispatches events, and it never closes the browser, the context or an
Instagram page. Only page.url and read-only page.evaluate.

Every value is redacted INSIDE the page before it crosses the CDP boundary:
text becomes a token (TEXT(12), REACTED_PHRASE, ...) and attribute values become
a shape (digits(17), idlike(17), /direct/t/<digits>/, text(18)). Snapshot files
therefore cannot contain a name, handle, message text, id value or id-bearing
URL. Attribute NAMES are kept - they are the evidence P01/P02 need.

Since P02 the parser run here is the PRODUCTION one (`shadow.dom`), not a local
copy: there is no second copy of the reading rules in this tool any more, so a
diagnostic cannot disagree with the runtime.

    check                    connection + page shape summary
    validate                 run the production reader on the open view
    snapshot --label NAME    structural snapshot into the private data dir
    fixture-check PATH.html  run the production reader on a LOCAL fixture
    shot --label NAME        viewport screenshot into the private data dir

Removed in P02: `open-row` and `scroll-pane`. Both drove the owner's page
(clicking a row, wheeling the pane); navigation and interaction now belong to
`shadow.instagram` behind a counted, guarded API, not to a diagnostic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shadow.browser import host_allowed  # noqa: E402
from shadow.dom import (  # noqa: E402
    JS_RAW, interpret_inbox, interpret_read, thread_id_from_url,
)
from shadow.instagram import InstagramBrowser  # noqa: E402

# Hardcoded on purpose: this tool must only ever reach the owner's own browser
# on loopback. There is no --endpoint flag.
CDP = "http://127.0.0.1:9222"

MAX_ROWS = 60
MAX_MESSAGES = 500
MAX_DESCENDANTS = 25
MAX_ANCESTOR_DEPTH = 12

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

# The structural extractor. Read-only: it reads attributes, geometry and text,
# and writes nothing anywhere - not even a page global, which the legacy
# parsers did (see FINDINGS.md, "Exception").
_JS_DISCOVER = r"""(opts) => {
    const full = !!(opts && opts.full);

    // --- redaction ------------------------------------------------------
    const KNOWN = new Set(['direct', 'inbox', 't', 'reel', 'reels', 'p', 'tv',
        'explore', 'accounts', 'login', 'challenge', 'checkpoint', 'suspended',
        'stories', 'highlights', 'chat', 'settings', 'about']);
    const THREAD_AFTER = new Set(['t']);
    const MEDIA_AFTER = new Set(['reel', 'reels', 'p', 'tv']);

    function pathShape(v) {
        let s = String(v == null ? '' : v);
        if (/^[a-z]+:\/\//i.test(s)) s = s.replace(/^[a-z]+:\/\/[^/]+/i, '');
        s = s.split('#')[0].split('?')[0];
        if (!s.startsWith('/')) return null;
        const raw = s.split('/');
        const out = [];
        for (let i = 0; i < raw.length; i++) {
            const seg = raw[i];
            if (seg === '') { if (i === raw.length - 1) out.push(''); continue; }
            if (THREAD_AFTER.has(seg) || MEDIA_AFTER.has(seg)) { out.push(seg); continue; }
            const prev = raw[i - 1];
            if (THREAD_AFTER.has(prev)) { out.push('<digits>'); continue; }
            if (MEDIA_AFTER.has(prev)) { out.push('<id>'); continue; }
            if (KNOWN.has(seg.toLowerCase())) { out.push(seg.toLowerCase()); continue; }
            if (/^\d+$/.test(seg)) { out.push('<digits>'); continue; }
            out.push('<handle>');
        }
        return out.join('/') || '/';
    }

    function shape(v) {
        if (v === null || v === undefined) return null;
        const s = String(v);
        if (s === '') return 'empty';
        const d = s.match(/\d{6,}/);
        if (d) return 'digits(' + d[0].length + ')';
        const i = s.match(/[A-Za-z0-9_-]{16,}/);
        if (i) return 'idlike(' + i[0].length + ')';
        if (/^[a-z]+:\/\//i.test(s) || s.startsWith('//')) return 'url(' + s.length + ')';
        if (s.startsWith('/')) { const p = pathShape(s); if (p) return p; }
        return 'text(' + s.length + ')';
    }

    const PATH_ATTRS = new Set(['href', 'action', 'data-href', 'data-url']);
    function attrShape(name, v) {
        if (PATH_ATTRS.has(name)) { const p = pathShape(v); if (p) return p; }
        return shape(v);
    }

    // Which named pattern a string matches - the pattern name only, never the
    // string. This is how a finding can say "this full-width leaf is a date
    // separator carrying a clock time" without recording a single character.
    const CLASSES = [
        ['you_prefix', /^You:/i],
        ['you_sent_attachment', /^You sent an attachment/i],
        ['reacted_to_your_message', /reacted .{0,8} to your message/i],
        ['reacted_any', /reacted/i],
        ['liked_a_message', /^\s*liked a message\s*$/i],
        ['sent_an_attachment', /sent an attachment/i],
        ['unread_word', /(^|\s)Unread(\s|$)/],
        ['active_now', /active now/i],
        ['active_any', /active/i],
        ['seen', /^seen/i],
        ['delivered', /^delivered/i],
        ['sent_word', /^\s*sent\s*$/i],
        ['edited', /^(edited|·|•|\.)$/i],
        ['replied_to_you', /replied to (you|yourself)/i],
        ['you_replied', /^you replied to /i],
        ['date_sep_weekday_time', /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+\d{1,2}:\d{2}/i],
        ['date_sep_d_month_time', /^\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{1,2}:\d{2}/],
        ['date_sep_month_d_time', /^[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{1,2}:\d{2}/],
        ['date_sep_d_month', /^\d{1,2}\s+[A-Za-z]{3,9},?\s*\d{0,4}$/],
        ['date_sep_month_d', /^[A-Za-z]{3,9}\s+\d{1,2},?\s*\d{0,4}$/],
        ['today_yesterday', /^(today|yesterday)$/i],
        ['clock', /^\d{1,2}:\d{2}(\s?[AP]M)?$/i],
        ['clock_range', /\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}/],
        ['clock_anywhere', /\d{1,2}:\d{2}/],
        ['age', /^\d+[smhdw]$/i],
        ['story_unavailable', /^story unavailable$/i],
        ['post_reel_unavailable', /^(post|reel|story) unavailable$/i],
        ['handle_at', /@[A-Za-z0-9._]+/],
        ['emoji_only', /^[^A-Za-z0-9]+$/],
    ];
    function classify(raw) {
        const f = String(raw || '').replace(/\s+/g, ' ').trim();
        if (!f) return [];
        const hits = [];
        for (const [name, re] of CLASSES) if (re.test(f)) hits.push(name);
        return hits;
    }

    // Character-class signature: which kinds of character a string is made of,
    // never which characters. "Name - 5:14 PM" -> "Aaaa - d:d AA". It is what
    // lets a finding say "the accessible name contains a clock time" without
    // recording the name or the time.
    function sig(s) {
        const f = String(s || '').replace(/\s+/g, ' ').trim();
        if (!f) return null;
        let out = '', prev = '';
        for (const ch of f) {
            const c = /\d/.test(ch) ? 'd' : /[a-z]/.test(ch) ? 'a' : /[A-Z]/.test(ch) ? 'A'
                : ch === ' ' ? 's' : /[:]/.test(ch) ? ':' : /[.,]/.test(ch) ? 'p'
                : /[-–—]/.test(ch) ? '-' : /[/]/.test(ch) ? '/' : 'o';
            if (c !== prev) { out += c; prev = c; }
        }
        return out.slice(0, 48);
    }

    function tokenText(raw) {
        const f = String(raw || '').replace(/\s+/g, ' ').trim();
        if (!f) return null;
        if (/^You:/i.test(f)) return 'YOU_PREFIX';
        if (/^unread$/i.test(f)) return 'UNREAD';
        if (/^·$/.test(f)) return 'AGE';
        if (/^\d+[smhdw]$/i.test(f)) return 'AGE';
        if (/sent an attachment/i.test(f)) return 'ATTACHMENT_PHRASE';
        if (/reacted/i.test(f)) return 'REACTED_PHRASE';
        if (/active/i.test(f)) return 'ACTIVE_PHRASE';
        if (/^seen/i.test(f)) return 'SEEN_PHRASE';
        if (/^\d{1,2}:\d{2}(\s?[AP]M)?$/i.test(f)) return 'TIME';
        if (/^\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}/.test(f)) return 'TIME';
        if (/^(today|yesterday|earlier|mon|tue|wed|thu|fri|sat|sun)[a-z]*(\s+\d{1,2}(:\d{2})?\s*(am|pm)?)$/i.test(f)) return 'DATE_SEPARATOR';
        if (!/[A-Za-z0-9]/.test(f)) return 'EMOJI_ONLY';
        return 'TEXT(' + f.length + ')';
    }

    // "·" separates the age from the preview inside one innerText line, so each
    // side is tokenized on its own - otherwise "· 5m" hides both tokens.
    function tokenLines(raw) {
        const out = [];
        for (const line of String(raw || '').split('\n')) {
            for (const part of line.split(/\s*·\s*/)) {
                const t = tokenText(part);
                if (t) out.push(t);
            }
        }
        return out;
    }

    // Instagram renders emoji as <img alt="...">, which innerText omits.
    function rawText(el) {
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
    }

    function attrNames(el) {
        try { return [...el.attributes].map(a => a.name); } catch (e) { return []; }
    }

    function labelInfo(el) {
        for (const attr of ['aria-labelledby', 'aria-describedby', 'aria-label']) {
            const v = el.getAttribute && el.getAttribute(attr);
            if (!v) continue;
            const raw = attr === 'aria-label' ? v
                : (() => { const t = document.getElementById(v.split(/\s+/)[0]);
                           return t ? (t.innerText || rawText(t)) : ''; })();
            if (!raw) return null;
            return {tokens: tokenLines(raw), sig: sig(raw), classes: classify(raw)};
        }
        return null;
    }

    function idAttrs(el) {
        const out = [];
        for (const a of [...el.attributes]) {
            const s = shape(a.value);
            if (s && (s.startsWith('digits(') || s.startsWith('idlike(')))
                out.push({name: a.name, shape: s});
        }
        return out;
    }

    function attrShapeMap(el) {
        const out = {};
        for (const a of el.attributes) out[a.name] = attrShape(a.name, a.value);
        return out;
    }

    function hrefShapes(el) {
        const out = [];
        for (const a of (el.querySelectorAll ? el.querySelectorAll('a[href]') : [])) {
            const p = pathShape(a.getAttribute('href'));
            if (p) out.push(p);
        }
        return out;
    }

    // --- page level ----------------------------------------------------
    const body = document.body ? document.body.innerText || '' : '';
    const path = location.pathname;
    const out = {
        page: {
            path_shape: pathShape(path) || 'text(' + path.length + ')',
            host_is_instagram: /(^|\.)instagram\.com$/i.test(location.hostname),
            seen_text_present: /(^|\n)\s*seen\b/i.test(body),
            login_or_challenge:
                /\/accounts\/login|\/challenge|\/checkpoint|\/suspended/i.test(path),
            viewport: {
                w: document.documentElement.clientWidth,
                h: document.documentElement.clientHeight,
            },
        },
    };

    // --- thread list ---------------------------------------------------
    const list = document.querySelector('[aria-label="Thread list"]');
    if (!list) {
        out.thread_list = {present: false};
    } else {
        const band = [...list.querySelectorAll('div[role="button"]')].filter(el => {
            const h = el.getBoundingClientRect().height;
            return h >= 60 && h <= 90;
        });
        const rows = band.filter(el => !band.some(o => o !== el && el.contains(o)));
        const recorded = [];
        for (const el of rows.slice(0, MAX_ROWS)) {
            const lines = tokenLines(el.innerText);
            if (!lines.length) continue;
            const ancestors = [];
            let a = el.parentElement, depth = 1;
            while (a && a !== list && depth <= 12) {
                ancestors.push({
                    depth: depth++,
                    tag: a.tagName,
                    role: a.getAttribute('role') || null,
                    attr_names: attrNames(a),
                    attr_shapes: full ? attrShapeMap(a) : undefined,
                    id_attrs: idAttrs(a),
                });
                a = a.parentElement;
            }
            const descendants = [];
            const walkDown = (node, d) => {
                if (d > 3 || descendants.length >= MAX_DESCENDANTS) return;
                for (const c of node.children) {
                    descendants.push({
                        depth: d,
                        tag: c.tagName,
                        role: c.getAttribute('role') || null,
                        attr_names: attrNames(c),
                        attr_shapes: full ? attrShapeMap(c) : undefined,
                        id_attrs: idAttrs(c),
                    });
                    walkDown(c, d + 1);
                }
            };
            walkDown(el, 1);
            recorded.push({
                tag: el.tagName,
                role: el.getAttribute('role') || null,
                attr_names: attrNames(el),
                attr_shapes: full ? attrShapeMap(el) : undefined,
                id_attrs: idAttrs(el),
                href_shapes: hrefShapes(el),
                pressed: el.getAttribute('aria-pressed') === 'true',
                ancestors: ancestors,
                descendants: descendants,
                lines: lines,
            });
        }
        out.thread_list = {
            present: true,
            container_role: list.getAttribute('role') || null,
            container_attr_names: attrNames(list),
            container_id_attrs: idAttrs(list),
            row_count: rows.length,
            band_div_count: list.querySelectorAll('div[role="button"]').length,
            rows: full ? recorded : [],
            rows_recorded: full ? recorded.length : 0,
        };
    }

    // --- message pane --------------------------------------------------
    // Same rule as the production reader: the widest scrollable container to the
    // right of the thread list.
    const vw = document.documentElement.clientWidth;
    let pane = null;
    for (const el of document.querySelectorAll('div')) {
        if (el.scrollHeight <= el.clientHeight + 40) continue;
        if (el.clientHeight < 200) continue;
        const r = el.getBoundingClientRect();
        if (r.left < vw * 0.15) continue;
        if (!pane || r.width > pane.getBoundingClientRect().width) pane = el;
    }

    if (!pane) {
        out.pane = {present: false};
        out.header = {present: false};
        out.messages = {present: false, count: 0, items: []};
        out.articles = {count: 0, recorded: 0, items: []};
    } else {
        const pr = pane.getBoundingClientRect();
        const paneL = pr.left, paneR = pr.right, paneW = pr.width;

        const roleCounts = {};
        for (const el of pane.querySelectorAll('[role]')) {
            const k = el.getAttribute('role');
            roleCounts[k] = (roleCounts[k] || 0) + 1;
        }
        const linkShapes = {};
        for (const a of pane.querySelectorAll('a[href]')) {
            const p = pathShape(a.getAttribute('href')) || shape(a.getAttribute('href'));
            linkShapes[p] = (linkShapes[p] || 0) + 1;
        }

        const articleEls = [...pane.querySelectorAll('[role="article"]')];
        const leaves = [...pane.querySelectorAll('[dir="auto"]')]
            .filter(el => el.querySelector('[dir="auto"]') === null);

        function articleIndexOf(el) {
            let a = el.parentElement;
            while (a && a !== pane) {
                const i = articleEls.indexOf(a);
                if (i !== -1) return i;
                a = a.parentElement;
            }
            return null;
        }

        const items = [];
        for (const el of leaves) {
            if (items.length >= (full ? MAX_MESSAGES : 0)) break;
            const r = el.getBoundingClientRect();
            const laid = r.width > 0 && r.height > 0;
            let side = 'unlaid';
            if (laid) {
                side = r.width > paneW * 0.9 ? 'full-width'
                    : ((r.left + r.width / 2) > (paneL + paneR) / 2 ? 'right' : 'left');
            }
            const ancestors = [];
            let a = el.parentElement, depth = 1;
            while (a && a !== pane && depth <= 12) {
                ancestors.push({
                    depth: depth++,
                    tag: a.tagName,
                    role: a.getAttribute('role') || null,
                    attr_names: attrNames(a),
                    attr_shapes: full ? attrShapeMap(a) : undefined,
                    id_attrs: idAttrs(a),
                    label: full ? labelInfo(a) : undefined,
                });
                a = a.parentElement;
            }
            items.push({
                index: items.length,
                side: side,
                width_ratio: laid ? Math.round((r.width / paneW) * 100) / 100 : null,
                laid: laid,
                attr_names: attrNames(el),
                attr_shapes: full ? attrShapeMap(el) : undefined,
                id_attrs: idAttrs(el),
                ancestors: ancestors,
                text_token: tokenText(rawText(el)),
                text_sig: full ? sig(rawText(el)) : undefined,
                classes: full ? classify(rawText(el)) : undefined,
                in_article: full ? articleIndexOf(el) : undefined,
                label: full ? labelInfo(el) : undefined,
            });
        }

        const articles = [];
        for (const el of articleEls) {
            if (!full) break;
            if (articles.length >= MAX_MESSAGES) continue;
            const r = el.getBoundingClientRect();
            const laid = r.width > 0 && r.height > 0;
            let side = 'unlaid';
            if (laid) {
                side = r.width > paneW * 0.9 ? 'full-width'
                    : ((r.left + r.width / 2) > (paneL + paneR) / 2 ? 'right' : 'left');
            }
            let aImgAlt = 0, aImg = 0;
            for (const i of el.querySelectorAll('img')) { aImg++; if (i.getAttribute('alt')) aImgAlt++; }
            const aLinks = {};
            for (const l of el.querySelectorAll('a[href]')) {
                const p = pathShape(l.getAttribute('href')) || shape(l.getAttribute('href'));
                aLinks[p] = (aLinks[p] || 0) + 1;
            }
            articles.push({
                index: articles.length,
                side: side,
                width_ratio: laid ? Math.round((r.width / paneW) * 100) / 100 : null,
                attr_names: attrNames(el),
                attr_shapes: attrShapeMap(el),
                id_attrs: idAttrs(el),
                label: labelInfo(el),
                text_sig: sig(rawText(el)),
                classes: classify(rawText(el)),
                leaf_dir_auto: el.querySelectorAll('[dir="auto"]').length,
                img_alt: aImgAlt,
                img_total: aImg,
                link_shapes: aLinks,
                full_text_tokens: tokenLines(rawText(el)),
            });
        }

        const headLinks = {};
        let headAvatars = 0, headProfileLinks = 0;
        for (const a of document.querySelectorAll('a[href]')) {
            const r = a.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            if (r.left < vw * 0.15) continue;
            if (r.top > pr.top + 140) continue;
            const p = pathShape(a.getAttribute('href'));
            if (!p) continue;
            headLinks[p] = (headLinks[p] || 0) + 1;
            headProfileLinks++;
            if (a.querySelector('img')) headAvatars++;
        }

        out.pane = {
            present: true,
            found_by: 'widest-scrollable-right-of-list',
            rect: {left: Math.round(paneL), right: Math.round(paneR), width: Math.round(paneW)},
            scroll_top: Math.round(pane.scrollTop),
            at_start: pane.scrollTop <= 2,
            attr_names: attrNames(pane),
            id_attrs: idAttrs(pane),
            role_counts: roleCounts,
            role_article_count: pane.querySelectorAll('[role="article"]').length,
            role_group_count: pane.querySelectorAll('[role="group"]').length,
            link_shapes: linkShapes,
            leaf_dir_auto: leaves.length,
        };
        out.header = {
            present: true,
            profile_link_shapes: headLinks,
            profile_link_count: headProfileLinks,
            avatar_images: headAvatars,
        };
        out.messages = {
            present: true,
            count: leaves.length,
            recorded: items.length,
            items: items,
        };
        out.articles = {
            count: pane.querySelectorAll('[role="article"]').length,
            recorded: articles.length,
            items: articles,
        };
    }

    // --- left column (thread list and section headers) -------------------
    const nav = [];
    for (const el of document.querySelectorAll('[role="button"],[role="tab"],a,li')) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (r.left > vw * 0.15) continue;
        const lines = tokenLines(el.innerText || '');
        if (!lines.length) continue;
        nav.push({
            tag: el.tagName,
            role: el.getAttribute('role') || null,
            attr_names: attrNames(el),
            height: Math.round(r.height),
            top: Math.round(r.top),
            lines: lines,
            href_shapes: hrefShapes(el),
        });
    }
    out.nav = {count: nav.length, items: full ? nav.slice(0, 60) : []};
    return out;
}"""


def _private_dir() -> Path:
    base = Path(
        os.environ.get("SHADOW_DATA_DIR")
        or (Path.home() / "Library/Application Support/Shadow")
    ).expanduser()
    private = base / "private" / "p00b"
    for p in (base, base / "private", private):
        p.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    return private


def _write_snapshot(label: str, data: dict) -> Path:
    snaps = _private_dir() / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    os.chmod(snaps, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = snaps / f"{label}-{stamp}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=True, sort_keys=True)
    os.chmod(path, 0o600)
    return path


def _interpret_counts(raw: dict) -> dict:
    inbox = interpret_inbox(raw)
    read = interpret_read(raw, thread_id_from_url(
        (raw.get("path") or "")))
    by_sender: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for event in read.events:
        by_sender[event.sender] = by_sender.get(event.sender, 0) + 1
        by_kind[event.kind] = by_kind.get(event.kind, 0) + 1
    return {
        "inbox": {
            "status": inbox.status,
            "rows": len(inbox.rows),
            "unread_hint": sum(1 for r in inbox.rows if r.unread_hint),
            "preview_classes": sorted({r.preview_class for r in inbox.rows}),
            "labels_not_unique": sum(1 for r in inbox.rows if not r.label_is_unique),
        },
        "read": {
            "status": read.status,
            "events": len(read.events),
            "by_sender": by_sender,
            "by_kind": by_kind,
            "media_labels": sorted({e.media_label for e in read.events
                                    if e.media_label}),
            "complete": read.complete,
            "is_group": read.is_group,
            "participant_links": read.participant_links,
            "composer_empty": not read.composer_text,
        },
    }


def _summary(data: dict) -> list[str]:
    page = data.get("page", {})
    tl = data.get("thread_list", {})
    pane = data.get("pane", {})
    msgs = data.get("messages", {})
    head = data.get("header", {})
    out = [
        f"path shape            {page.get('path_shape')}",
        f"login/challenge       {page.get('login_or_challenge')}",
        f"SEEN text present     {page.get('seen_text_present')}",
        f"thread list present   {tl.get('present')}",
    ]
    if tl.get("present"):
        out.append(f"thread rows          {tl.get('row_count')}"
                   f"  (band divs {tl.get('band_div_count')})")
    out += [
        f"message pane present  {pane.get('present')}",
        f"leaf [dir=auto]       {msgs.get('count')}",
        f"header profile links  {head.get('profile_link_count')}"
        f"  avatars {head.get('avatar_images')}",
    ]
    if pane.get("present"):
        out.append(f"pane scroll top      {pane.get('scroll_top')}"
                   f"  at_start {pane.get('at_start')}")
        out.append(f"role=article/group  {pane.get('role_article_count')}"
                   f" / {pane.get('role_group_count')}")
    reader = data.get("production_reader")
    if reader:
        inbox, read = reader["inbox"], reader["read"]
        out += [
            f"reader inbox        {inbox['status']}  rows {inbox['rows']}"
            f"  unread_hint {inbox['unread_hint']}  ambiguous {inbox['labels_not_unique']}",
            f"reader read         {read['status']}  events {read['events']}"
            f"  by_sender {read['by_sender']}",
            f"reader by kind      {read['by_kind']}",
            f"reader media        {read['media_labels']}",
            f"reader complete     {read['complete']}  is_group {read['is_group']}"
            f"  participant_links {read['participant_links']}",
            f"composer empty      {read['composer_empty']}",
        ]
    return out


async def _discover(page, full: bool) -> dict:
    data = await page.evaluate(_JS_DISCOVER, {"full": full})
    raw = await page.evaluate(JS_RAW)
    data["production_reader"] = _interpret_counts(raw)
    return data


async def _one_instagram_page(pw) -> object:
    browser = await pw.chromium.connect_over_cdp(CDP)
    pages = []
    for ctx in browser.contexts:
        for page in ctx.pages:
            try:
                url = page.url or ""
            except Exception:
                continue
            if host_allowed(url):
                pages.append(page)
    if len(pages) != 1:
        print(
            f"found {len(pages)} Instagram pages, need exactly 1.\n"
            "Leave exactly one Instagram tab open in the dedicated window "
            "(close other instagram.com tabs, keep the window itself open).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return pages[0]


async def cmd_check() -> int:
    pw = await async_playwright().start()
    try:
        page = await _one_instagram_page(pw)
        data = await _discover(page, full=False)
    except SystemExit:
        raise
    except Exception as e:
        print(f"connected no ({type(e).__name__}: {str(e)[:120]})")
        return 1
    finally:
        await pw.stop()
    print("connected yes")
    print("instagram pages 1")
    for line in _summary(data):
        print(line)
    return 0


async def cmd_validate() -> int:
    """Run the production reader on whatever the owner has open. No clicks.

    The open thread is read FIRST and costs no navigation, then the inbox is
    read. That order is deliberate: reading the inbox first would navigate away
    from the thread the owner opened. Navigation counts are printed, never
    hidden.
    """
    adapter = InstagramBrowser(cdp_url=CDP)
    try:
        started = await adapter.start()
    except Exception as e:
        print(f"validate failed to bind ({type(e).__name__}: {str(e)[:120]})")
        return 1
    if adapter.page is None:
        print(f"bind                {started.status}  {started.detail}")
        return 1
    print(f"bind                {started.status}  {started.detail}")

    thread_id = thread_id_from_url(adapter.page.url or "")
    if thread_id:
        read = await adapter.read_thread(thread_id)
        counts = _interpret_counts(await adapter.page.evaluate(JS_RAW))
        detail = counts["read"]
        print(f"read                {read.status}  navigations {read.navigations_used}")
        print(f"  events            {len(read.events)}  by_sender {detail['by_sender']}")
        print(f"  by kind           {detail['by_kind']}  media {detail['media_labels']}")
        print(f"  complete          {read.complete}  is_group {read.is_group}"
              f"  participant_links {read.participant_links}")
        print(f"  composer          {'empty' if not read.composer_text else 'NON-EMPTY'}")
        print(f"  detail            {read.detail}")
    else:
        print("read                no thread open; nothing was opened")

    inbox = await adapter.inbox()
    print(f"inbox               {inbox.status}  navigations {inbox.navigations_used}")
    print(f"  rows              {len(inbox.rows)}"
          f"  unread_hint {sum(1 for r in inbox.rows if r.unread_hint)}"
          f"  ambiguous {sum(1 for r in inbox.rows if not r.label_is_unique)}")
    if inbox.detail:
        print(f"  detail            {inbox.detail}")
    await adapter.close()
    return 0


async def cmd_snapshot(label: str) -> int:
    if not _LABEL_RE.match(label):
        print("--label must be [A-Za-z0-9_-], max 40 chars", file=sys.stderr)
        return 2
    pw = await async_playwright().start()
    try:
        page = await _one_instagram_page(pw)
        data = await _discover(page, full=True)
    except SystemExit:
        raise
    except Exception as e:
        print(f"snapshot failed ({type(e).__name__}: {str(e)[:120]})")
        return 1
    finally:
        await pw.stop()
    data["label"] = label
    data["captured_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["note"] = (
        "text tokenized and attribute values shaped in-page; names, handles, "
        "message text, id values and id-bearing URLs are not present. "
        "production_reader is the output of shadow.dom, not a local copy."
    )
    path = _write_snapshot(label, data)
    print(f"label  {label}")
    for line in _summary(data):
        print(line)
    print(f"written {path} (0600)")
    return 0


async def cmd_fixture_check(path_str: str) -> int:
    """Run the production reader on a local fictional fixture."""
    path = Path(path_str).expanduser().resolve()
    if not path.is_file():
        print(f"no such fixture: {path}", file=sys.stderr)
        return 2
    if path.suffix.lower() not in (".html", ".htm"):
        print("fixture-check only reads .html files", file=sys.stderr)
        return 2

    pw = await async_playwright().start()
    try:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:
            print(f"headless launch failed ({type(e).__name__}: {str(e)[:80]})")
            browser = None
        if browser is not None:
            page = await browser.new_page()
            try:
                await page.goto(path.as_uri(), wait_until="load")
                raw = await page.evaluate(JS_RAW)
            finally:
                await browser.close()
        else:
            connected = await pw.chromium.connect_over_cdp(CDP)
            ctx = connected.contexts[0]
            page = await ctx.new_page()
            try:
                await page.goto(path.as_uri(), wait_until="load")
                raw = await page.evaluate(JS_RAW)
            finally:
                await page.close()   # only the tab this command opened
    except SystemExit:
        raise
    except Exception as e:
        print(f"fixture-check failed ({type(e).__name__}: {str(e)[:160]})")
        return 1
    finally:
        await pw.stop()

    inbox = interpret_inbox(raw)
    read = interpret_read(raw, None)
    print(f"fixture        {path.name}")
    print(f"inbox          {inbox.status}  rows {len(inbox.rows)}"
          f"  unread_hint {sum(1 for r in inbox.rows if r.unread_hint)}")
    for r in inbox.rows:
        print(f"   {r.preview_class:16s} unique={r.label_is_unique}"
              f"  {r.preview_text!r}")
    print(f"read           {read.status}  events {len(read.events)}"
          f"  complete {read.complete}  is_group {read.is_group}"
          f"  participant_links {read.participant_links}")
    for e in read.events:
        print(f"   {e.position:3d} {e.sender:7s} {e.kind:8s} laid={e.laid_out}"
              f" media={e.media_label} reply={e.reply_to_text!r} {e.text!r}")
    return 0


async def cmd_shot(label: str) -> int:
    """Screenshot the viewport via raw CDP.

    Page.captureScreenshot only reads pixels, so unlike page.screenshot it
    never touches the viewport. The image lands in the private directory and is
    never committed; the terminal only gets the path and the size.
    """
    if not _LABEL_RE.match(label):
        print("--label must be [A-Za-z0-9_-], max 40 chars", file=sys.stderr)
        return 2
    pw = await async_playwright().start()
    try:
        page = await _one_instagram_page(pw)
        cdp = await page.context.new_cdp_session(page)
        try:
            res = await cdp.send("Page.captureScreenshot", {"format": "png"})
        finally:
            await cdp.detach()
    except SystemExit:
        raise
    except Exception as e:
        print(f"screenshot failed ({type(e).__name__}: {str(e)[:120]})")
        return 1
    finally:
        await pw.stop()
    import base64

    shots = _private_dir() / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    os.chmod(shots, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = shots / f"{label}-{stamp}.png"
    blob = base64.b64decode(res["data"])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    os.chmod(path, 0o600)
    print(f"label {label}  {len(blob)} bytes  {path}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="connection and page shape summary")
    sub.add_parser(
        "validate",
        help="run the production reader on the open view; no clicks, no typing",
    )
    s = sub.add_parser("snapshot", help="structural snapshot (redacted) into the private dir")
    s.add_argument("--label", required=True)
    f = sub.add_parser("fixture-check", help="run the production reader on a local .html fixture")
    f.add_argument("path")
    s2 = sub.add_parser("shot", help="viewport screenshot into the private dir")
    s2.add_argument("--label", required=True)
    args = ap.parse_args()
    handlers = {
        "check": lambda: cmd_check(),
        "validate": lambda: cmd_validate(),
        "snapshot": lambda: cmd_snapshot(args.label),
        "fixture-check": lambda: cmd_fixture_check(args.path),
        "shot": lambda: cmd_shot(args.label),
    }
    raise SystemExit(asyncio.run(handlers[args.cmd]()))


if __name__ == "__main__":
    main()

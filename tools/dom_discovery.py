#!/usr/bin/env python3
"""P00b — read-only live DOM discovery for the dedicated Instagram profile.

Connects to the owner's Chrome for Testing profile over CDP and READS the
rendered DOM. It never navigates, reloads, clicks, taps, fills, types, presses,
hovers, scrolls, focuses or dispatches events, and it never closes the browser,
the context or an Instagram page. Only page.url and read-only page.evaluate.

Every value is redacted INSIDE the page before it crosses the CDP boundary:
text becomes a token (TEXT(12), REACTED_PHRASE, ...) and attribute values become
a shape (digits(17), idlike(17), /direct/t/<digits>/, text(18)). Snapshot files
therefore cannot contain a name, handle, message text, id value or id-bearing
URL. Attribute NAMES are kept — they are the evidence P01 needs.

    check                    connection + page shape summary
    snapshot --label NAME    structural snapshot into the private data dir
    fixture-check PATH.html  run the production parsers on a LOCAL fixture
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

from shadow.browser import (  # noqa: E402
    _JS_HAS_PANE, _JS_INBOX, _JS_PANE_CENTRE, _JS_READ, host_allowed,
)

# Hardcoded on purpose: this tool must only ever reach the owner's own browser
# on loopback. There is no --endpoint flag.
CDP = "http://127.0.0.1:9222"

MAX_ROWS = 60
MAX_MESSAGES = 500
MAX_DESCENDANTS = 25
MAX_ANCESTOR_DEPTH = 12

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

# The whole extractor. Read-only: it reads attributes, geometry and text, and
# the only thing it writes anywhere is nothing — the two production parsers it
# also runs set window.__shSender (a page-local Map memo), which is recorded in
# FINDINGS.md as an accepted exception.
_JS_DISCOVER = r"""(opts) => {
    const full = !!(opts && opts.full);

    // --- redaction ------------------------------------------------------
    const KNOWN = new Set(['direct', 'inbox', 't', 'reel', 'reels', 'p', 'tv',
        'explore', 'accounts', 'login', 'challenge', 'suspended', 'stories',
        'highlights', 'chat', 'settings', 'about']);
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

    // Which named pattern a string matches — the pattern name only, never the
    // string. This is how a finding can say "this full-width leaf is a date
    // separator carrying a clock time" and "the production NOISE list drops it
    // by text" without recording a single character.
    const CLASSES = [
        ['you_prefix', /^You:/i],
        ['you_sent_attachment', /^You sent an attachment/i],
        ['reacted_to_your_message', /Reacted .{0,4} to your message/i],
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

    // Copy of the production NOISE list in shadow/browser.py _JS_READ, so each
    // leaf can be reported as dropped-by-text or kept. A copy can drift from
    // the original; if P02 changes that list, re-check this one.
    const PROD_NOISE = [
        /^.{1,60}replied to (you|yourself)\s*([·•].*)?$/i,
        /^.{1,40} replied to you$/i,
        /^you replied to /i,
        /^replied to @/i,
        /^(edited|·|•|\.|Unread)$/i,
        /^\d{1,2}:\d{2}\s*(AM|PM)$/i,
        /^\d+[smhdw]$/i,
        /^(liked a message|you reacted|you unsent|message unavailable|Seen|Sent|Delivered|Active now)$/i,
        /^(Yesterday|Today)$/i,
        /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}:\d{2}/i,
        /^(story unavailable|reel unavailable|post unavailable)$/i,
    ];
    function prodNoise(raw) {
        const f = (raw || '').replace(/\s*\n\s*/g, ' ').trim();
        if (!f) return {noise: true, rule: -1};   // empty text is noise too
        for (let i = 0; i < PROD_NOISE.length; i++) {
            if (PROD_NOISE[i].test(f)) return {noise: true, rule: i};
        }
        return {noise: false, rule: -1};
    }

    // Character-class signature: which kinds of character a string is made of,
    // never which characters. "Name · 5:14 PM" -> "Aaaa · d:d AA". It is what
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

    const isDateSep = f =>
        /^(today|yesterday|earlier|mon|tue|wed|thu|fri|sat|sun)[a-z]*(\s+\d{1,2}(:\d{2})?\s*(am|pm)?)?$/i.test(f) ||
        /^\d{1,2}\s+[a-z]{3,9}$/i.test(f) ||
        /^[a-z]{3,9}\s+\d{1,2},?(\s*\d{4})?$/i.test(f);

    function tokenText(raw) {
        const f = (raw || '').replace(/\s+/g, ' ').trim();
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
        if (isDateSep(f)) return 'DATE_SEPARATOR';
        if (!/[A-Za-z0-9]/.test(f)) return 'EMOJI_ONLY';
        return 'TEXT(' + f.length + ')';
    }

    // "·" separates the age from the preview inside one innerText line, so each
    // side is tokenized on its own — otherwise "· 5m" hides both tokens.
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

    // The text an aria-labelledby / aria-describedby / aria-label points at,
    // tokenized line by line. Instagram puts the accessible name of a message
    // on another element, so its tokens are the only per-message metadata the
    // DOM offers (a TIME or AGE token here would be a real per-message stamp).
    function labelInfo(el) {
        for (const attr of ['aria-labelledby', 'aria-describedby', 'aria-label']) {
            const v = el.getAttribute && el.getAttribute(attr);
            if (!v) continue;
            const raw = attr === 'aria-label' ? v
                : (() => { const t = document.getElementById(v.split(/\s+/)[0]);
                           return t ? (t.innerText || rawText(t)) : ''; })();
            if (!raw) continue;
            return {tokens: tokenLines(raw), sig: sig(raw), classes: classify(raw)};
        }
        return null;
    }

    // Which attribute carries an id-like value, and what shape it has.
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
        for (const a of [...el.attributes]) out[a.name] = attrShape(a.name, a.value);
        return out;
    }

    function hrefShapes(el) {
        const out = [];
        for (const a of el.querySelectorAll ? el.querySelectorAll('a[href]') : []) {
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
                /\/accounts\/login|\/challenge|\/suspended/i.test(path),
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
        const rows = [];
        for (const el of list.querySelectorAll('div')) {
            const r = el.getBoundingClientRect();
            if (r.height < 60 || r.height > 90) continue;
            const lines = tokenLines(el.innerText);
            if (lines.length < 2) continue;
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
                if (d > 3 || descendants.length >= 25) return;
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
            rows.push({
                index: rows.length,
                tag: el.tagName,
                role: el.getAttribute('role') || null,
                attr_names: attrNames(el),
                attr_shapes: attrShapeMap(el),
                id_attrs: idAttrs(el),
                href_shapes: hrefShapes(el),
                ancestors: ancestors,
                descendants: descendants,
                lines: lines,
                line_sigs: String(el.innerText || '').split('\n')
                    .map(l => sig(l)).filter(Boolean),
                line_classes: String(el.innerText || '').split('\n')
                    .map(l => classify(l)).filter(c => c.length),
            });
        }
        out.thread_list = {
            present: true,
            container_role: list.getAttribute('role') || null,
            container_attr_names: attrNames(list),
            container_id_attrs: idAttrs(list),
            row_count: rows.length,
            rows: full ? rows.slice(0, 60) : [],
            rows_recorded: full ? Math.min(rows.length, 60) : 0,
        };
    }

    // --- message pane --------------------------------------------------
    // Same rule as _JS_READ / _JS_HAS_PANE: the widest scrollable container to
    // the right of the thread list.
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
        const chatMid = (paneL + paneR) / 2;

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
        let imgAlt = 0, imgTotal = 0;
        for (const i of pane.querySelectorAll('img')) {
            imgTotal++;
            if (i.getAttribute('alt')) imgAlt++;
        }

        const articleEls = [...pane.querySelectorAll('[role="article"]')];
        const leaves = [...pane.querySelectorAll('[dir="auto"]')]
            .filter(el => el.querySelector('[dir="auto"]') === null);

        // Which message container, if any, a [dir=auto] leaf belongs to. The
        // production reader treats every leaf as a message, so the leaves that
        // are NOT in an article are the ones it must be discarding by width.
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
        let truncated = 0;
        for (const el of leaves) {
            if (items.length >= (full ? 500 : 0)) { truncated++; continue; }
            const r = el.getBoundingClientRect();
            const laid = r.width > 0 && r.height > 0;
            let side = 'unlaid';
            if (laid) {
                if (r.width > paneW * 0.9) side = 'full-width';
                else side = (r.left + r.width / 2) > chatMid ? 'right' : 'left';
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
            let mImgAlt = 0, mImg = 0;
            for (const i of el.querySelectorAll('img')) { mImg++; if (i.getAttribute('alt')) mImgAlt++; }
            const mLinks = {};
            for (const l of el.querySelectorAll('a[href]')) {
                const p = pathShape(l.getAttribute('href')) || shape(l.getAttribute('href'));
                mLinks[p] = (mLinks[p] || 0) + 1;
            }
            items.push({
                index: items.length,
                side: side,
                width_ratio: laid ? Math.round((r.width / paneW) * 100) / 100 : null,
                laid: laid,
                top: laid ? Math.round(r.top) : null,
                attr_names: attrNames(el),
                attr_shapes: full ? attrShapeMap(el) : undefined,
                id_attrs: idAttrs(el),
                ancestors: ancestors,
                text_token: tokenText(rawText(el)),
                text_sig: full ? sig(rawText(el)) : undefined,
                classes: full ? classify(rawText(el)) : undefined,
                prod_noise: full ? prodNoise(rawText(el)).noise : undefined,
                prod_noise_rule: full ? prodNoise(rawText(el)).rule : undefined,
                in_article: full ? articleIndexOf(el) : undefined,
                label: full ? labelInfo(el) : undefined,
                img_alt: mImgAlt,
                img_total: mImg,
                link_shapes: mLinks,
            });
        }

        // role="article" is the per-message container the [dir=auto] leaf sits
        // in. Recorded separately: it is the element P02 would anchor on, and
        // its accessible name is the only per-message metadata the DOM offers.
        const articles = [];
        for (const el of pane.querySelectorAll('[role="article"]')) {
            if (!full) break;
            if (articles.length >= 500) continue;
            const r = el.getBoundingClientRect();
            const laid = r.width > 0 && r.height > 0;
            let side = 'unlaid';
            if (laid) {
                if (r.width > paneW * 0.9) side = 'full-width';
                else side = (r.left + r.width / 2) > chatMid ? 'right' : 'left';
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
                label_classes: labelInfo(el) ? labelInfo(el).classes || null : null,
                text_sig: sig(rawText(el)),
                classes: classify(rawText(el)),
                leaf_dir_auto: el.querySelectorAll('[dir="auto"]').length,
                img_alt: aImgAlt,
                img_total: aImg,
                link_shapes: aLinks,
                full_text_tokens: tokenLines(rawText(el)),
            });
        }

        // Header: the strip above the message list, right of the thread list.
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
            attr_names: attrNames(pane),
            id_attrs: idAttrs(pane),
            role_counts: roleCounts,
            role_article_count: pane.querySelectorAll('[role="article"]').length,
            role_group_count: pane.querySelectorAll('[role="group"]').length,
            link_shapes: linkShapes,
            img_total: imgTotal,
            img_with_alt: imgAlt,
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
            truncated: truncated,
            items: items,
        };
        out.articles = {
            count: pane.querySelectorAll('[role="article"]').length,
            recorded: articles.length,
            items: articles,
        };
    }

    // --- left column (thread list + section headers + Requests entry) -----
    // Read-only inventory of the left nav: which section headers exist, where
    // the Requests entry sits and what it looks like, without clicking it.
    const nav = [];
    for (const el of document.querySelectorAll('[role="button"],[role="tab"],a,li')) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (r.left > vw * 0.15) continue;
        const raw = el.innerText || '';
        const lines = tokenLines(raw);
        if (!lines.length) continue;
        nav.push({
            tag: el.tagName,
            role: el.getAttribute('role') || null,
            attr_names: attrNames(el),
            height: Math.round(r.height),
            top: Math.round(r.top),
            lines: lines,
            line_sigs: String(raw).split('\n').map(l => sig(l)).filter(Boolean),
            line_classes: String(raw).split('\n').map(l => classify(l)).filter(c => c.length),
            href_shapes: hrefShapes(el),
        });
    }
    out.nav = {count: nav.length, items: full ? nav.slice(0, 60) : []};

    // --- production parsers: counts only --------------------------------
    if (full) {
        let inbox = {available: false};
        try {
            const rows = eval('(' + opts.inbox + ')')();
            inbox = {
                available: true,
                rows: rows.length,
                unread: rows.filter(r => r.unread).length,
                with_name_token: rows.filter(r => r.name).length,
                all_thread_ids_empty: rows.every(r => !r.thread_id),
                preview_tokens: tokenLines(rows.map(r => r.last_text).join('\n')).length,
            };
        } catch (e) { inbox = {available: false, error: String(e).slice(0, 120)}; }

        let read = {available: false};
        try {
            const rows = eval('(' + opts.read + ')')();
            const byType = {};
            for (const r of rows) byType[r.type] = (byType[r.type] || 0) + 1;
            read = {
                available: true,
                messages: rows.length,
                me: rows.filter(r => r.sender === 'me').length,
                them: rows.filter(r => r.sender === 'them').length,
                by_type: byType,
                empty_contents: rows.filter(r => !(r.content || '').trim()).length,
                unlaid_top: rows.filter(r => !r.top).length,
            };
        } catch (e) { read = {available: false, error: String(e).slice(0, 120)}; }

        out.production_parser = {inbox: inbox, read: read};
    }

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


async def _discover(page, full: bool) -> dict:
    return await page.evaluate(
        _JS_DISCOVER, {"full": full, "inbox": _JS_INBOX, "read": _JS_READ}
    )


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
        out.append(f"thread rows          {tl.get('row_count')}")
    out += [
        f"message pane present  {pane.get('present')}",
        f"leaf [dir=auto]       {msgs.get('count')}",
        f"header profile links  {head.get('profile_link_count')}",
    ]
    if pane.get("present"):
        out.append(f"pane img / w/ alt    {pane.get('img_total')} / {pane.get('img_with_alt')}")
        out.append(f"role=article/group  {pane.get('role_article_count')} / {pane.get('role_group_count')}")
        out.append(f"pane role counts      {len(pane.get('role_counts', {}))} distinct")
    pp = data.get("production_parser")
    if pp:
        inbox, read = pp.get("inbox", {}), pp.get("read", {})
        out += [
            f"_JS_INBOX rows       {inbox.get('rows')} (unread {inbox.get('unread')})",
            f"_JS_READ messages    {read.get('messages')} "
            f"(me {read.get('me')} / them {read.get('them')})",
            f"_JS_READ by type     {read.get('by_type')}",
            f"_JS_READ empty       {read.get('empty_contents')}",
        ]
    return out


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
        "message text, id values and id-bearing URLs are not present"
    )
    path = _write_snapshot(label, data)
    print(f"label  {label}")
    for line in _summary(data):
        print(line)
    print(f"written {path} (0600)")
    return 0


async def cmd_fixture_check(path_str: str) -> int:
    path = Path(path_str).expanduser().resolve()
    if not path.is_file():
        print(f"no such fixture: {path}", file=sys.stderr)
        return 2
    if path.suffix.lower() not in (".html", ".htm"):
        print("fixture-check only reads .html files", file=sys.stderr)
        return 2
    url = path.as_uri()
    launch_js = r"""(o) => {
        const out = {};
        let inbox = null;
        try { inbox = eval('(' + o.inbox + ')')(); } catch (e) { out.inbox_error = String(e).slice(0, 120); }
        out.inbox = inbox;
        let read = null;
        try { read = eval('(' + o.read + ')')(); } catch (e) { out.read_error = String(e).slice(0, 120); }
        out.read = read;
        try { out.has_pane = eval('(' + o.haspane + ')')(); } catch (e) {}
        out.dir_auto = document.querySelectorAll('[dir="auto"]').length;
        return out;
    }"""

    async def run(page) -> dict:
        return await page.evaluate(
            launch_js,
            {"inbox": _JS_INBOX, "read": _JS_READ, "haspane": _JS_HAS_PANE},
        )

    pw = await async_playwright().start()
    try:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:
            print(f"headless launch failed ({type(e).__name__}: {str(e)[:80]});"
                  " using a new tab in the dedicated browser")
            browser = None
        if browser is not None:
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="load")
                result = await run(page)
            finally:
                await browser.close()
        else:
            connected = await pw.chromium.connect_over_cdp(CDP)
            ctx = connected.contexts[0]
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="load")
                result = await run(page)
            finally:
                await page.close()   # only the tab this command opened
    except SystemExit:
        raise
    except Exception as e:
        print(f"fixture-check failed ({type(e).__name__}: {str(e)[:160]})")
        return 1
    finally:
        await pw.stop()

    inbox = result.get("inbox") or []
    read = result.get("read") or []
    by_type: dict[str, int] = {}
    for r in read:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    print(f"fixture        {path.name}")
    print(f"has_pane       {result.get('has_pane')}   [dir=auto] {result.get('dir_auto')}")
    print(f"inbox rows     {len(inbox)}  unread {sum(1 for r in inbox if r['unread'])}")
    for r in inbox:
        print(f"   {r['name']!r}  unread={r['unread']}  {r['last_text']!r}")
    print(f"read messages  {len(read)}  me {sum(1 for r in read if r['sender'] == 'me')}"
          f"  them {sum(1 for r in read if r['sender'] == 'them')}")
    for r in read:
        print(f"   {r['sender']:4s} {r['type']:6s} top={r['top']:<5} {r['content']!r}")
    print(f"by type        {by_type}")
    print(f"empty content  {sum(1 for r in read if not (r['content'] or '').strip())}")
    return 0


# The row list, filtered the way the production inbox parser identifies a row,
# but keeping only role="button" rows (one per thread) instead of every 60-90px
# wrapper. Used to enumerate rows and to click exactly one of them.
_JS_ROWS = r"""() => {
    const list = document.querySelector('[aria-label="Thread list"]');
    if (!list) return [];
    const out = [];
    for (const el of list.querySelectorAll('div[role="button"]')) {
        const r = el.getBoundingClientRect();
        if (r.height < 60 || r.height > 90) continue;
        const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
        if (lines.length < 2) continue;
        out.push({y: Math.round(r.top), lines: lines.length, height: Math.round(r.height)});
    }
    return out.sort((a, b) => a.y - b.y);
}"""


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


async def cmd_open_row(index: int) -> int:
    """Open the Nth thread row (0 = top of the list) and wait for the pane.

    This is the one navigation the tool performs, and only on the owner's
    instruction: it clicks a thread row. It sends nothing, likes nothing,
    follows nothing, opens no story, and touches no request.
    """
    pw = await async_playwright().start()
    try:
        page = await _one_instagram_page(pw)
        rows = await page.evaluate(_JS_ROWS)
        if not rows:
            print("no thread rows on screen")
            return 1
        if not 0 <= index < len(rows):
            print(f"row {index} out of range; {len(rows)} rows on screen", file=sys.stderr)
            return 2
        before = len(rows)
        await page.evaluate(
            """(i) => {
                const list = document.querySelector('[aria-label="Thread list"]');
                const rows = [...list.querySelectorAll('div[role="button"]')].filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.height >= 60 && r.height <= 90;
                }).sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
                if (rows[i]) rows[i].click();
            }""",
            index,
        )
        try:
            await page.wait_for_function(_JS_HAS_PANE, timeout=10_000)
        except Exception:
            print("pane did not appear after opening the row")
        await page.wait_for_timeout(1200)
        data = await _discover(page, full=True)
    except SystemExit:
        raise
    except Exception as e:
        print(f"open-row failed ({type(e).__name__}: {str(e)[:120]})")
        return 1
    finally:
        await pw.stop()
    print(f"opened row {index} of {before}")
    for line in _summary(data):
        print(line)
    return 0


async def cmd_scroll_pane(times: int, on_list: bool) -> int:
    """Wheel the message pane (or the thread list) up.

    Trusted wheel events are the only thing Instagram loads more history for;
    assigning scrollTop in JS does nothing.
    """
    js = (
        """() => {
            const l = document.querySelector('[aria-label="Thread list"]');
            if (!l) return null;
            const r = l.getBoundingClientRect();
            return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
        }"""
        if on_list
        else _JS_PANE_CENTRE
    )
    pw = await async_playwright().start()
    try:
        page = await _one_instagram_page(pw)
        centre = await page.evaluate(js)
        if not centre:
            print("no scrollable message pane" if not on_list else "no thread list")
            return 1
        cdp = await page.context.new_cdp_session(page)
        try:
            for _ in range(times):
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseWheel", "x": centre["x"], "y": centre["y"],
                     "deltaX": 0, "deltaY": -2500, "pointerType": "mouse"},
                )
                await page.wait_for_timeout(600)
        finally:
            await cdp.detach()
        await page.wait_for_timeout(1200)
        data = await _discover(page, full=True)
    except SystemExit:
        raise
    except Exception as e:
        print(f"scroll failed ({type(e).__name__}: {str(e)[:120]})")
        return 1
    finally:
        await pw.stop()
    print(f"scrolled {'thread list' if on_list else 'message pane'} up {times}x")
    for line in _summary(data):
        print(line)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="connection and page shape summary")
    s = sub.add_parser("snapshot", help="structural snapshot (redacted) into the private dir")
    s.add_argument("--label", required=True)
    f = sub.add_parser("fixture-check", help="run production parsers on a local .html fixture")
    f.add_argument("path")
    s2 = sub.add_parser("shot", help="viewport screenshot into the private dir")
    s2.add_argument("--label", required=True)
    o = sub.add_parser("open-row", help="open the Nth thread row (0 = top)")
    o.add_argument("--index", type=int, required=True)
    w = sub.add_parser("scroll-pane", help="wheel the message pane up N times")
    w.add_argument("--times", type=int, default=1)
    w.add_argument("--list", action="store_true", help="scroll the thread list instead")
    args = ap.parse_args()
    handlers = {
        "check": lambda: cmd_check(),
        "snapshot": lambda: cmd_snapshot(args.label),
        "fixture-check": lambda: cmd_fixture_check(args.path),
        "shot": lambda: cmd_shot(args.label),
        "open-row": lambda: cmd_open_row(args.index),
        "scroll-pane": lambda: cmd_scroll_pane(args.times, args.list),
    }
    raise SystemExit(asyncio.run(handlers[args.cmd]()))


if __name__ == "__main__":
    main()

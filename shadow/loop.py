"""The loop. Replaces 746 lines of overlapping state machines.

State is one file: {"replied": {person name: sha1(last inbound text)},
"stories_date": "YYYY-MM-DD"}. No State enum, no active-thread pointer,
no pending queue, no starvation timer, no worker class.

The guard is keyed on the person's NAME rather than thread_id, because
thread_id is not in the inbox DOM - it is only learned after clicking the row.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from datetime import date, datetime

from shadow import brain, memory
from shadow.browser import Browser, NotLoggedIn
from shadow.config import (
    ACTIVE_END, ACTIVE_START, GAP_BETWEEN_CYCLES, GAP_BETWEEN_THREADS,
    HISTORY_LIMIT, READ_DEPTH, STATE_FILE, STORIES_PER_DAY,
)
from shadow.memory import Msg

log = logging.getLogger("shadow.loop")

# Matches the labels browser._label() puts on shared media, e.g.
# "[reel from @handle]", "[post]", "[video from @x] caption".
_CARD_RE = re.compile(r"^\[(reel|post|video)\b[^\]]*\]", re.I)


# --- state -----------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state file corrupt, starting fresh")
    return {"replied": {}, "stories_date": ""}


def save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def already_replied(state: dict, who: str, last_text: str) -> bool:
    """True if we already answered this exact inbound message from this person."""
    return state["replied"].get(who) == _hash(last_text)


def mark_replied(state: dict, who: str, last_text: str) -> None:
    state["replied"][who] = _hash(last_text)
    # One entry per person, overwritten each time, so this is bounded by the
    # size of the inbox rather than by runtime. Trim anyway in case a display
    # name changes and leaves an orphan behind.
    if len(state["replied"]) > 500:
        state["replied"] = dict(list(state["replied"].items())[-250:])
        log.info("trimmed replied-guard state to 250 entries")
    save_state(state)


def in_active_hours(now: datetime | None = None) -> bool:
    return ACTIVE_START <= (now or datetime.now()).hour < ACTIVE_END


def stories_due(state: dict) -> bool:
    return state.get("stories_date") != date.today().isoformat()


def is_card(content: str) -> bool:
    """True if this 'message' is a shared reel/post rather than text.

    browser._label() renders those as "[reel from @handle]". They carry no
    words to respond to, so treating them as text makes the model echo the
    poster's handle back.
    """
    return content.strip().startswith("[") and _CARD_RE.match(content.strip()) is not None


# --- one cycle -------------------------------------------------------

async def cycle(br: Browser, state: dict, dry_run: bool = False) -> int:
    """One pass over the unread inbox. Returns how many threads were replied to."""
    replied = 0

    for t in await br.threads():
        if not t.unread:
            continue
        if not t.last_text:
            log.info("skip %s: no preview text to key the guard on", t.name)
            continue
        # The guard is keyed on NAME, not thread_id: thread_id is unknown until
        # the row is clicked, so keying on it would collide every thread on "".
        if already_replied(state, t.name, t.last_text):
            continue

        try:
            await br.open(t)                     # click by name, learn thread_id
        except Exception as e:
            log.error("could not open %s: %s", t.name, e)
            continue

        msgs = await br.read(t.thread_id, want=READ_DEPTH)
        if not msgs:
            log.warning("skip %s: read returned nothing", t.name)
            continue

        # The bug from gpt/things-to-change.md: the old worker never checked
        # this and could reply to its own last message.
        if msgs[-1].sender != "them":
            log.info("skip %s: last message is mine", t.name)
            mark_replied(state, t.name, t.last_text)
            continue

        # A shared reel is not a question. When the last thing they sent is a
        # card, there is no text to answer, and the model parrots the poster's
        # handle back ("ekmeva secretary", "tmkoceditss.i") because that is the
        # only text in the prompt. Nemotron cannot watch the reel, so the
        # honest behaviour is to say nothing.
        if is_card(msgs[-1].content):
            log.info("skip %s: last message is a shared reel/post, nothing to reply to",
                     t.name)
            mark_replied(state, t.name, t.last_text)
            continue

        # Same problem one level up: if there is no real text from them at all
        # in the window, a reply would be invented rather than responsive.
        if not any(m.sender == "them" and not is_card(m.content) for m in msgs):
            log.info("skip %s: no text from them in the last %d messages",
                     t.name, len(msgs))
            mark_replied(state, t.name, t.last_text)
            continue

        prof = memory.profile(t.name)
        past = memory.history(t.name, HISTORY_LIMIT)
        d = brain.reply(t.name, prof, msgs, past)

        if d.action == "freeze":
            log.warning("FROZEN %s [%s] — not replying, read the log", t.name, d.note)
            mark_replied(state, t.name, t.last_text)
            continue
        if d.action != "reply":
            log.info("skip %s: %s", t.name, d.note)
            mark_replied(state, t.name, t.last_text)
            continue

        if dry_run:
            print(f"\n--- {t.name} ---")
            for m in msgs[-4:]:
                who = "them" if m.sender == "them" else "me  "
                print(f"  {who}: {m.content[:100]}")
            print("  WOULD SEND:")
            for m in d.messages:
                print(f"    > {m}")
            continue

        if await br.send_burst(d.messages):
            memory.append(t.name, msgs + [Msg("me", m) for m in d.messages])
            mark_replied(state, t.name, t.last_text)
            replied += 1
        else:
            log.error("send failed for %s — not saving to memory", t.name)

        await asyncio.sleep(random.uniform(*GAP_BETWEEN_THREADS))

    return replied


async def run(once: bool = False, dry_run: bool = False) -> None:
    state = load_state()
    br = Browser()
    await br.start()

    try:
        while True:
            if not in_active_hours():
                log.info("outside active hours (%02d-%02d), sleeping", ACTIVE_START, ACTIVE_END)
                if once:
                    return
                await asyncio.sleep(60)
                continue

            try:
                n = await cycle(br, state, dry_run)
                if n:
                    log.info("cycle done: %d replied", n)

                if stories_due(state) and not dry_run:
                    if await br.stories(STORIES_PER_DAY):
                        state["stories_date"] = date.today().isoformat()
                        save_state(state)

            except NotLoggedIn:
                raise  # not recoverable by waiting
            except Exception as e:
                log.exception("cycle failed: %s", e)
                if once:
                    raise
                await asyncio.sleep(60)

            if once:
                return
            await asyncio.sleep(random.uniform(*GAP_BETWEEN_CYCLES))
    finally:
        await br.close()

"""Interactive browser driver — YOU type the commands, you judge the result.

The point: the browser layer has never touched real Instagram. Last time an
agent reported "everything works" and nothing did. So this tool does not
decide anything. Every command prints what it actually found, and you decide
whether that matches what is on your screen.

It drives the SAME code the bot uses (shadow/browser.py) — not a copy — so a
pass here means the real thing works.

Nothing is ever sent. `send` is not implemented on purpose.

SETUP
  1. Close Chrome completely.
  2. Start it with debugging on, logged into Instagram:
       "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222
  3. python drive.py

WHAT YOU SEE
  Chrome comes to the front on every command, and the bot draws on the real
  page so you are not trusting the terminal:

    thread rows   blue box = read, RED box = unread, numbered like the terminal
    messages      GREEN box = it thinks YOU sent it, AMBER = them
    stories       purple box = it would view this one
    black bar     what the bot just parsed, at the top of the page

  Boxes are drawn from what the code RETURNED, not from its selectors. So a
  box in the wrong place means the bot really is wrong. Reload or `clear` to
  remove them.

COMMANDS
  inbox              scan the DM list + box every thread it found
  open <n|id>        open thread by row number from `inbox`, or by raw id
  read               re-read the open thread + box each message by sender
  stories            list unwatched stories + box them in the tray
  story <user|n>     open one story
  profile [user]     open a profile (default: your own)
  home               instagram.com
  where              current URL + whether it is on-site
  shot               screenshot to drive-shots/ (keeps the boxes)
  clear              remove the boxes
  guard <url>        try to navigate somewhere - proves confinement works
  help               this list
  q                  quit
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from datetime import datetime
from pathlib import Path

from shadow import browser as B
from shadow.browser import Browser, NotLoggedIn, OffSite

SHOTS = Path("drive-shots")


# --- visual feedback -------------------------------------------------
# Drawn from the data production code ACTUALLY returned, never from a copy of
# its selectors. If the bot mis-parses something, the box lands in the wrong
# place — which is the whole point. All of it is temporary page decoration and
# vanishes on reload.

_JS_CLEAR = """() => {
    document.querySelectorAll('.__sh').forEach(e => e.remove());
}"""

_JS_HUD = """(text) => {
    document.querySelectorAll('.__sh-hud').forEach(e => e.remove());
    const d = document.createElement('div');
    d.className = '__sh __sh-hud';
    d.textContent = text;
    Object.assign(d.style, {
        position: 'fixed', top: '10px', left: '50%',
        transform: 'translateX(-50%)', zIndex: '2147483647',
        background: 'rgba(10,10,10,.92)', color: '#fff',
        font: '600 13px/1.5 system-ui, sans-serif',
        padding: '8px 16px', borderRadius: '8px',
        border: '1px solid #444', pointerEvents: 'none',
        maxWidth: '90vw', whiteSpace: 'pre-wrap', textAlign: 'center',
    });
    document.body.appendChild(d);
}"""

# Outline the thread rows the bot actually returned, by thread_id.
_JS_BOX_THREADS = """(items) => {
    document.querySelectorAll('.__sh-box').forEach(e => e.remove());
    let drawn = 0;
    for (const it of items) {
        const a = document.querySelector(`a[href*="/direct/t/${it.id}"]`);
        if (!a) continue;
        const r = a.getBoundingClientRect();
        if (!r.width) continue;
        const b = document.createElement('div');
        b.className = '__sh __sh-box';
        Object.assign(b.style, {
            position: 'fixed', left: r.left + 'px', top: r.top + 'px',
            width: r.width + 'px', height: r.height + 'px',
            border: `2px solid ${it.unread ? '#ff3b30' : '#0a84ff'}`,
            borderRadius: '6px', zIndex: '2147483646',
            pointerEvents: 'none', boxSizing: 'border-box',
        });
        const tag = document.createElement('div');
        tag.className = '__sh __sh-box';
        tag.textContent = it.n + (it.unread ? ' UNREAD' : '');
        Object.assign(tag.style, {
            position: 'fixed', left: r.left + 'px', top: (r.top - 2) + 'px',
            background: it.unread ? '#ff3b30' : '#0a84ff', color: '#fff',
            font: '700 10px/1.4 system-ui, sans-serif', padding: '1px 5px',
            borderRadius: '4px', zIndex: '2147483647', pointerEvents: 'none',
        });
        document.body.appendChild(b);
        document.body.appendChild(tag);
        drawn++;
    }
    return drawn;
}"""

# Outline each parsed message, coloured by who the bot thinks sent it.
# Green = "me", amber = "them". Wrong colour on screen == wrong split.
_JS_BOX_MSGS = """(items) => {
    document.querySelectorAll('.__sh-box').forEach(e => e.remove());
    const leaves = [...document.querySelectorAll('[dir="auto"]')]
        .filter(el => el.querySelector('[dir="auto"]') === null);
    const used = new Set();
    let drawn = 0;
    for (const it of items) {
        const el = leaves.find(e => !used.has(e) &&
            (e.innerText || '').trim() === it.text);
        if (!el) continue;
        used.add(el);
        const r = el.getBoundingClientRect();
        if (!r.width) continue;
        const me = it.sender === 'me';
        const col = me ? '#30d158' : '#ff9f0a';
        const b = document.createElement('div');
        b.className = '__sh __sh-box';
        Object.assign(b.style, {
            position: 'fixed', left: (r.left - 3) + 'px', top: (r.top - 2) + 'px',
            width: (r.width + 6) + 'px', height: (r.height + 4) + 'px',
            border: `2px solid ${col}`, borderRadius: '10px',
            zIndex: '2147483646', pointerEvents: 'none', boxSizing: 'border-box',
        });
        const tag = document.createElement('div');
        tag.className = '__sh __sh-box';
        tag.textContent = me ? 'ME' : 'THEM';
        Object.assign(tag.style, {
            position: 'fixed',
            left: (me ? r.right - 34 : r.left - 3) + 'px',
            top: (r.top - 13) + 'px',
            background: col, color: '#000',
            font: '700 9px/1.4 system-ui, sans-serif', padding: '1px 4px',
            borderRadius: '3px', zIndex: '2147483647', pointerEvents: 'none',
        });
        document.body.appendChild(b);
        document.body.appendChild(tag);
        drawn++;
    }
    return drawn;
}"""

# Outline the story avatars the bot picked as unwatched.
_JS_BOX_STORIES = """(users) => {
    document.querySelectorAll('.__sh-box').forEach(e => e.remove());
    let drawn = 0;
    users.forEach((u, i) => {
        const a = document.querySelector(`a[href*="/stories/${u}/"]`);
        if (!a) return;
        const t = a.closest('li, button, [role="button"]') || a;
        const r = t.getBoundingClientRect();
        if (!r.width) return;
        const b = document.createElement('div');
        b.className = '__sh __sh-box';
        Object.assign(b.style, {
            position: 'fixed', left: r.left + 'px', top: r.top + 'px',
            width: r.width + 'px', height: r.height + 'px',
            border: '2px solid #bf5af2', borderRadius: '8px',
            zIndex: '2147483646', pointerEvents: 'none', boxSizing: 'border-box',
        });
        const tag = document.createElement('div');
        tag.className = '__sh __sh-box';
        tag.textContent = String(i + 1);
        Object.assign(tag.style, {
            position: 'fixed', left: r.left + 'px', top: (r.top - 12) + 'px',
            background: '#bf5af2', color: '#fff',
            font: '700 10px/1.4 system-ui, sans-serif', padding: '1px 5px',
            borderRadius: '4px', zIndex: '2147483647', pointerEvents: 'none',
        });
        document.body.appendChild(b);
        document.body.appendChild(tag);
        drawn++;
    });
    return drawn;
}"""

BANNER = """
------------------------------------------------------------------
 SHADOW BROWSER DRIVER - you drive, you judge
------------------------------------------------------------------
 Chrome must already be running with --remote-debugging-port=9222
 and logged into Instagram. Nothing will be sent. Ever.

 Type `help` for commands, `q` to quit.
------------------------------------------------------------------"""


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "q"


class Driver:
    def __init__(self) -> None:
        self.br = Browser()
        self.threads: list[B.Thread] = []
        self.stories: list[str] = []

    # --- visual helpers ----------------------------------------------

    async def _js(self, script: str, arg=None):
        """Run overlay JS. Never let decoration break a command."""
        try:
            if arg is None:
                return await self.br.page.evaluate(script)
            return await self.br.page.evaluate(script, arg)
        except Exception as e:
            log_line = f"  (overlay failed: {type(e).__name__})"
            print(log_line)
            return None

    async def _hud(self, text: str) -> None:
        await self._js(_JS_HUD, text)

    async def _clear(self) -> None:
        await self._js(_JS_CLEAR)

    async def _focus(self) -> None:
        """Pull the Chrome window forward so you can watch it work."""
        try:
            await self.br.page.bring_to_front()
        except Exception:
            pass

    # --- commands ----------------------------------------------------

    async def cmd_inbox(self) -> None:
        await self._focus()
        self.threads = await self.br.threads()
        if not self.threads:
            await self._hud("SHADOW: parsed 0 threads - selector may be wrong")
            print("  (no threads found - is the inbox actually empty?)")
            return

        # Box every row the bot returned, on the real page.
        drawn = await self._js(_JS_BOX_THREADS, [
            {"id": t.thread_id, "n": i, "unread": t.unread}
            for i, t in enumerate(self.threads, 1)
        ])
        n_unread = sum(t.unread for t in self.threads)
        await self._hud(
            f"SHADOW parsed {len(self.threads)} threads, {n_unread} unread\n"
            f"blue = read, red = unread. Numbers match the terminal rows."
        )
        print(f"\n  drew {drawn or 0} boxes on the page - LOOK AT CHROME")
        print(f"  {len(self.threads)} threads, {n_unread} unread\n")
        print(f"  {'#':>3}  {'unread':6}  {'thread_id':20}  name / last message")
        print(f"  {'-'*3}  {'-'*6}  {'-'*20}  {'-'*40}")
        for i, t in enumerate(self.threads, 1):
            flag = "UNREAD" if t.unread else ""
            print(f"  {i:>3}  {flag:6}  {t.thread_id:20}  {t.name[:24]}"
                  + (f"  |  {t.last_text[:40]}" if t.last_text else ""))
        print("\n  CHECK ON SCREEN: is there a box around every conversation?")
        print("  A row with no box = the bot cannot see it. A red box on an")
        print("  already-read chat = the unread flag is wrong.")

    async def cmd_open(self, arg: str) -> None:
        if not arg:
            print("  usage: open <row number from `inbox`>  or  open <thread_id>")
            return
        if arg.isdigit() and len(arg) <= 3 and self.threads:
            n = int(arg)
            if not 1 <= n <= len(self.threads):
                print(f"  no row {n} - run `inbox` first, it had {len(self.threads)}")
                return
            t = self.threads[n - 1]
            tid, who = t.thread_id, t.name
        else:
            tid, who = arg, "(raw id)"
        await self._focus()
        print(f"  opening {who} -> thread {tid}")
        msgs = await self.br.read(tid)
        print(f"  URL now: {self.br.page.url}")
        if not msgs:
            await self._hud(f"SHADOW: opened thread {tid} but parsed 0 messages")
            print("  NO MESSAGES READ. Either the thread is empty, or the "
                  "selector is wrong. Compare against your screen.")
            return
        await self._draw_msgs(msgs, who)
        self._show(msgs)

    async def cmd_read(self) -> None:
        await self._focus()
        url = self.br.page.url
        tid = B.thread_id_from_url(url)
        if not tid:
            print(f"  not in a thread (URL: {url})\n  use `open <n>` first")
            return
        msgs = await self.br.read(tid)
        if not msgs:
            await self._hud("SHADOW: parsed 0 messages in this thread")
            print("  NO MESSAGES READ - selector may be wrong.")
            return
        await self._draw_msgs(msgs, tid)
        self._show(msgs)

    async def _draw_msgs(self, msgs, who: str) -> None:
        """Box each parsed message, coloured by the sender the bot decided."""
        drawn = await self._js(_JS_BOX_MSGS, [
            {"text": m.content, "sender": m.sender} for m in msgs
        ])
        mine = sum(1 for m in msgs if m.sender == "me")
        await self._hud(
            f"SHADOW parsed {len(msgs)} messages in {who}\n"
            f"GREEN = it thinks you sent it, AMBER = them "
            f"({mine} me / {len(msgs) - mine} them)"
        )
        print(f"  drew {drawn or 0} of {len(msgs)} message boxes - LOOK AT CHROME")
        if drawn is not None and drawn < len(msgs):
            print(f"  ({len(msgs) - drawn} could not be matched back to an "
                  "element - usually duplicate text, not a parse failure)")

    def _show(self, msgs) -> None:
        print(f"\n  {len(msgs)} messages read (last 15):\n")
        for m in msgs[-15:]:
            who = "THEM" if m.sender == "them" else "me  "
            print(f"    {who} | {m.content[:78]}")
        mine = sum(1 for m in msgs if m.sender == "me")
        print(f"\n  {mine} mine / {len(msgs) - mine} theirs")
        print("  CHECK ON SCREEN: is every green box on YOUR side of the chat")
        print("  and every amber box on theirs? The split is decided by pixel")
        print("  position, so this is the most likely thing to be wrong.")

    async def cmd_stories(self) -> None:
        await self._focus()
        await self.br._goto(B.IG_HOME)
        try:
            await self.br.page.wait_for_selector('a[href*="/stories/"]', timeout=10_000)
        except Exception:
            await self._hud("SHADOW: no story tray found on home")
            print("  no story tray found on the home page")
            return
        self.stories = await self.br.page.evaluate(B._JS_STORY_USERS)
        if not self.stories:
            await self._hud("SHADOW: tray found, 0 unwatched stories extracted")
            print("  tray found but no unwatched stories extracted")
            return
        drawn = await self._js(_JS_BOX_STORIES, self.stories)
        await self._hud(
            f"SHADOW picked {len(self.stories)} unwatched stories\n"
            "purple boxes = it would view these"
        )
        print(f"\n  drew {drawn or 0} boxes - LOOK AT CHROME")
        print(f"  {len(self.stories)} unwatched stories:")
        for i, u in enumerate(self.stories, 1):
            print(f"    {i:>3}  @{u}")
        print("\n  CHECK ON SCREEN: do the boxed avatars have unseen rings?")
        print("  A boxed avatar you already watched = the seen-filter is wrong.")

    async def cmd_story(self, arg: str) -> None:
        if not arg:
            print("  usage: story <username>  or  story <n from `stories`>")
            return
        if arg.isdigit() and self.stories:
            n = int(arg)
            if not 1 <= n <= len(self.stories):
                print(f"  no story {n}")
                return
            user = self.stories[n - 1]
        else:
            user = arg.lstrip("@")
        await self._focus()
        await self.br._goto(B.IG_STORY.format(username=user))
        await self._hud(f"SHADOW opened @{user}'s story")
        print(f"  opened @{user}\n  URL: {self.br.page.url}")
        print("  CHECK ON SCREEN: is the story actually playing, and is it "
              "the right person?")

    async def cmd_profile(self, arg: str) -> None:
        await self._focus()
        user = arg.lstrip("@") if arg else ""
        url = f"https://www.instagram.com/{user}/" if user \
            else "https://www.instagram.com/"
        await self.br._goto(url)
        title = await self.br.page.title()
        await self._hud(f"SHADOW opened profile: {user or '(home)'}")
        print(f"  URL: {self.br.page.url}")
        print(f"  title: {title}")
        print("  CHECK ON SCREEN: is this the right profile?")

    async def cmd_home(self) -> None:
        await self._focus()
        await self.br._goto(B.IG_HOME)
        await self._hud("SHADOW opened the home feed")
        print(f"  URL: {self.br.page.url}")

    async def cmd_where(self) -> None:
        url = self.br.page.url
        on = B.host_allowed(url)
        print(f"  URL:     {url}")
        print(f"  on-site: {on}")
        tid = B.thread_id_from_url(url)
        if tid:
            print(f"  thread:  {tid}")

    async def cmd_shot(self) -> None:
        SHOTS.mkdir(exist_ok=True)
        p = SHOTS / f"{datetime.now():%H%M%S}.png"
        await self.br.page.screenshot(path=str(p))
        print(f"  saved {p}  (boxes included, so you can keep the evidence)")

    async def cmd_clear(self) -> None:
        await self._clear()
        print("  overlays removed (a page reload also clears them)")

    async def cmd_guard(self, arg: str) -> None:
        """Prove the confinement. This SHOULD fail."""
        target = arg or "https://example.com"
        print(f"  attempting navigation to {target}")
        try:
            await self.br._goto(target)
        except OffSite as e:
            print(f"  BLOCKED (correct): {e}")
            return
        except Exception as e:
            print(f"  failed with {type(e).__name__}: {str(e)[:100]}")
            return
        print(f"  *** NOT BLOCKED *** now at {self.br.page.url}")
        print("  *** THIS IS A BUG - confinement failed ***")

    # --- loop --------------------------------------------------------

    async def run(self) -> None:
        print(BANNER)
        try:
            await self.br.start()
        except RuntimeError as e:
            print(f"\n{e}\n")
            return
        await self.cmd_where()

        table = {
            "inbox": self.cmd_inbox, "read": self.cmd_read,
            "stories": self.cmd_stories, "home": self.cmd_home,
            "where": self.cmd_where, "shot": self.cmd_shot,
            "clear": self.cmd_clear,
        }
        with_arg = {
            "open": self.cmd_open, "story": self.cmd_story,
            "profile": self.cmd_profile, "guard": self.cmd_guard,
        }

        while True:
            raw = ask("\nshadow> ")
            if not raw:
                continue
            cmd, _, arg = raw.partition(" ")
            cmd, arg = cmd.lower(), arg.strip()

            if cmd in ("q", "quit", "exit"):
                break
            if cmd in ("help", "?", "h"):
                print(__doc__[__doc__.index("COMMANDS"):])
                continue
            if cmd == "send":
                print("  `send` is deliberately not implemented here. This "
                      "tool never sends a message.")
                continue

            fn = table.get(cmd)
            try:
                if fn:
                    await fn()
                elif cmd in with_arg:
                    await with_arg[cmd](arg)
                else:
                    print(f"  unknown command {cmd!r} - try `help`")
            except NotLoggedIn as e:
                print(f"  NOT LOGGED IN: {e}")
            except OffSite as e:
                print(f"  OFF-SITE BLOCKED: {e}")
            except Exception as e:
                print(f"  {type(e).__name__}: {e}")
                if "--debug" in sys.argv:
                    traceback.print_exc()

        await self.br.close()
        print("\ndisconnected (your Chrome is still open)")


if __name__ == "__main__":
    asyncio.run(Driver().run())

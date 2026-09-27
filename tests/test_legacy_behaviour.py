"""tests/test_legacy_behaviour.py — the legacy self-check, ported to unittest.

Every check from the old `python test_shadow.py` is here as a unittest method
with the same intent, except two that need the owner's real corpus; those moved
to tools/private_checks.py and are not part of the public suite.

No private file, contact name, handle or voice data is required. Anything
data-dependent runs against a throwaway temp directory. Source-text assertions
are grouped in LegacySourceTextTest at the bottom.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def source(name: str) -> str:
    return (REPO / "shadow" / name).read_text(encoding="utf-8")


class TempPeople:
    """Point shadow at a throwaway people directory for the duration of a test."""

    def setUp(self) -> None:
        import shadow.config as C
        import shadow.memory as M

        self._tmp = Path(tempfile.mkdtemp(prefix="legacy-people-"))
        self._real = C.PEOPLE_DIR
        C.PEOPLE_DIR = M.PEOPLE_DIR = self._tmp

    def tearDown(self) -> None:
        import shadow.config as C
        import shadow.memory as M

        C.PEOPLE_DIR = M.PEOPLE_DIR = self._real
        shutil.rmtree(self._tmp, ignore_errors=True)


class LegacyBehaviourTest(unittest.TestCase):
    def test_freeze_list(self) -> None:
        from shadow.brain import frozen

        self.assertEqual(frozen("bhai 500 rupee bhej de"), "money")
        self.assertEqual(frozen("i love you"), "confession")
        self.assertEqual(frozen("dad is in the hospital"), "emergency")
        self.assertEqual(frozen("been feeling depressed lately"), "emotional")
        # must not fire on ordinary talk
        self.assertIsNone(frozen("aabe kya kar rha"))
        self.assertIsNone(frozen("lol that reel was crazy"))
        self.assertIsNone(frozen(""))

    def test_replied_guard(self) -> None:
        from shadow.loop import already_replied, mark_replied

        state = {"replied": {}, "stories_date": ""}
        self.assertFalse(already_replied(state, "123", "hey"))

        # mark_replied writes to disk; patch save_state out for the unit test
        import shadow.loop as L

        real_save = L.save_state
        L.save_state = lambda s: None
        try:
            mark_replied(state, "123", "hey")
            self.assertTrue(already_replied(state, "123", "hey"))
            self.assertFalse(already_replied(state, "123", "hey again"))
            self.assertFalse(already_replied(state, "456", "hey"))
        finally:
            L.save_state = real_save

    def test_replied_guard_state_is_bounded(self) -> None:
        """The replied memo must not grow without limit."""
        import shadow.loop as L

        real_save = L.save_state
        L.save_state = lambda s: None
        try:
            state = {"replied": {f"person{i}": "x" for i in range(600)}, "stories_date": ""}
            L.mark_replied(state, "new", "hello")
            self.assertLessEqual(len(state["replied"]), 251, len(state["replied"]))
        finally:
            L.save_state = real_save

    def test_active_hours(self) -> None:
        from shadow.config import ACTIVE_END, ACTIVE_START
        from shadow.loop import in_active_hours

        self.assertTrue(in_active_hours(datetime(2026, 7, 25, ACTIVE_START)))
        self.assertFalse(in_active_hours(datetime(2026, 7, 25, ACTIVE_START - 1)))
        self.assertFalse(in_active_hours(datetime(2026, 7, 25, ACTIVE_END)))
        self.assertFalse(in_active_hours(datetime(2026, 7, 25, 3)))

    def test_stories_due(self) -> None:
        from shadow.loop import stories_due

        self.assertTrue(stories_due({"stories_date": ""}))
        self.assertTrue(stories_due({"stories_date": "2020-01-01"}))
        self.assertFalse(stories_due({"stories_date": date.today().isoformat()}))

    def test_last_sender_guard(self) -> None:
        """Never reply when the last message is mine."""
        from shadow.memory import Msg

        def should_reply(msgs: list) -> bool:
            return bool(msgs) and msgs[-1].sender == "them"

        self.assertTrue(should_reply([Msg("me", "yo"), Msg("them", "kya")]))
        self.assertFalse(should_reply([Msg("them", "kya"), Msg("me", "kuch nahi")]))
        self.assertFalse(should_reply([]))

    def test_safe_never_returns_empty(self) -> None:
        """A name with no alphanumerics used to sanitize to "", so PEOPLE_DIR / ""
        resolved to data/people/ itself: a contact whose display name was
        emoji-only had their profile written loose in the shared root, invisible
        to people() and waiting to be overwritten by the next such name."""
        from shadow.memory import _safe

        for name in ("❀", "...", "___", "", "!!!", "♡"):
            got = _safe(name)
            self.assertTrue(got, f"_safe({name!r}) returned empty")
            self.assertTrue(got.strip("._"), f"_safe({name!r}) -> {got!r} is path-unsafe")

        # distinct unnameable inputs must not collide onto one folder
        self.assertNotEqual(_safe("❀"), _safe("\U0001f525\U0001f4a5"))

        # names that already have folders on disk must be untouched
        for name in ("test_friend", "UPPERCASE_NAME", "Name_with_digits_7"):
            self.assertEqual(_safe(name), name)

    def test_junk_filter(self) -> None:
        """Content-free rows must not reach the model."""
        from shadow.memory import is_junk

        for j in ("[reel]", "[image]", "[voice note]", "[voicenote]", "[sticker]",
                  "[gif]", "[video]", "[link]", "[REEL]", "  [reel]  ",
                  "Reacted ❤️ to your message", "Liked a message",
                  "sent an attachment", "You sent an attachment.",
                  "This message was unsent", "", "   "):
            self.assertTrue(is_junk(j), f"{j!r} should be junk")

        # real messages must survive, including ones that merely mention media
        for ok in ("aabe", "?", "lol dekha wo reel", "send me the image",
                   "[reel] was funny", "kya"):
            self.assertFalse(is_junk(ok), f"{ok!r} should NOT be junk")

    def test_parse_output(self) -> None:
        """The legacy provider returned one message per line. No schema."""
        from shadow.brain import _parse

        self.assertEqual(_parse("aabe\nu alive\n?"), ["aabe", "u alive", "?"])
        self.assertEqual(_parse("  kya  \n\n\n  lol  "), ["kya", "lol"])

        # skip token, with or without stray punctuation
        self.assertEqual(_parse("NOREPLY"), [])
        self.assertEqual(_parse("noreply."), [])
        self.assertEqual(_parse(""), [])
        self.assertEqual(_parse("   \n  "), [])

        # reasoning tags stripped
        self.assertEqual(_parse("<think>they want plans</think>\nkal free hu"), ["kal free hu"])
        self.assertEqual(_parse("<THINK>x\ny</THINK>\nhaan"), ["haan"])

        # model dressing it up as a list or quoting it
        self.assertEqual(_parse("- aabe\n- kya"), ["aabe", "kya"])
        self.assertEqual(_parse("1. yo\n2) sup"), ["yo", "sup"])
        self.assertEqual(_parse('"kya kar rha"'), ["kya kar rha"])

        # hard cap at 4
        self.assertEqual(len(_parse("\n".join("m" + str(i) for i in range(9)))), 4)

    def test_parse_json_array(self) -> None:
        """A JSON array is the requested output shape; it gives the model a
        place to stop."""
        from shadow.brain import _parse

        self.assertEqual(_parse('["yea"]'), ["yea"])
        self.assertEqual(_parse('["nahi bhai", "kal dekhta hu"]'), ["nahi bhai", "kal dekhta hu"])
        self.assertEqual(_parse('[“yea”, “kya”]'), ["yea", "kya"])  # smart quotes
        self.assertEqual(_parse('["NOREPLY"]'), [])
        self.assertEqual(_parse('["\U0001f923\U0001f923"]'), ["\U0001f923\U0001f923"])
        # array cap
        self.assertEqual(len(_parse('["a","b","c","d","e","f"]')), 4)

    def test_parse_rejects_debris(self) -> None:
        """Malformed output must not leak into a real DM. This exact shape was
        produced in evaluation and survived to output before the guard existed."""
        from shadow.brain import _parse

        self.assertEqual(_parse('["Yea bhai",…….”\nựu"]'), [])
        self.assertEqual(_parse('["broken'), [])
        self.assertEqual(_parse('once upon a time",  "'), [])

    def test_parse_rejects_echo(self) -> None:
        """Repeating their message back, with or without an added '?', is not a
        reply — the model does this when it does not understand them."""
        from shadow.brain import _parse

        self.assertEqual(_parse('["If time permits?"]', "If time permits."), [])
        self.assertEqual(_parse('["Instagram yr?"]', "Instagram yr"), [])
        # a real question about what they said is fine
        self.assertEqual(_parse('["kya solution"]', "wait i have a solution"), ["kya solution"])

    def test_parse_stops_on_loop_and_meta(self) -> None:
        """Near the token cap the model repeats itself; it also sometimes narrates."""
        from shadow.brain import _parse

        self.assertEqual(_parse("ha bhai\nkya hua\nha bhai\nkya hua"), ["ha bhai", "kya hua"])
        self.assertEqual(_parse("Based on the style analysis:\nha bhai"), [])
        self.assertEqual(_parse("ha bhai\nthem: something"), ["ha bhai"])

    def test_nemotron_region_guard(self) -> None:
        """ap-south-1 does not serve Nemotron; config must redirect off it."""
        from shadow import config as c

        self.assertNotIn("ap-south-1", c.NEMOTRON_REGIONS)
        self.assertIn(c.NEMOTRON_DEFAULT_REGION, c.NEMOTRON_REGIONS)

    def test_domain_confinement(self) -> None:
        """The bot must never navigate off instagram.com. Note the suffix-match
        bypass: 'evilinstagram.com'.endswith('instagram.com') is True, which is
        how an allowlist like this usually gets broken."""
        from shadow.browser import OffSite, check_url, host_allowed

        for good in ("https://www.instagram.com/direct/inbox/",
                     "https://instagram.com/stories/foo/",
                     "https://www.instagram.com/direct/t/1234/"):
            self.assertTrue(host_allowed(good), good)

        for bad in ("https://evilinstagram.com/",            # suffix bypass
                    "https://instagram.com.evil.com/",       # prefix trick
                    "https://www.instagram.com.attacker.io/",
                    "http://www.instagram.com/",             # plain http
                    "https://l.instagram.com/?u=http://evil.com",  # link shim
                    "https://facebook.com/",
                    "javascript:alert(1)",
                    "data:text/html,<h1>x",
                    "file:///C:/Windows/"):
            self.assertFalse(host_allowed(bad), bad)
            with self.assertRaises(OffSite):
                check_url(bad)

    def test_card_detection(self) -> None:
        """A shared reel is not something to reply to. Without this the model
        parrots the poster's handle back, because that is the only text present."""
        from shadow.loop import is_card

        self.assertTrue(is_card("[reel from @fictional_creator]"))
        self.assertTrue(is_card("[post]"))
        self.assertTrue(is_card("[video from @x] some caption"))
        self.assertTrue(is_card("  [reel from @y]  "))
        self.assertFalse(is_card("hey bhai"))
        self.assertFalse(is_card("[not a card]"))
        self.assertFalse(is_card(""))
        # a real message that merely starts with a bracket
        self.assertFalse(is_card("[laughs] ok"))


class LegacyMemoryTest(TempPeople, unittest.TestCase):
    def test_memory_roundtrip(self) -> None:
        """append -> history round trip, against a temp data dir."""
        from shadow import memory as M

        M.append("Test Person", [M.Msg("them", "aabe"), M.Msg("me", "kya")])
        history = M.history("Test Person", 10)
        self.assertEqual([(m.sender, m.content) for m in history],
                         [("them", "aabe"), ("me", "kya")])
        self.assertEqual(M.profile("Test Person")["total_messages"], 2)

        M.append("Test Person", [M.Msg("them", "?")])
        self.assertEqual(M.profile("Test Person")["total_messages"], 3)
        self.assertEqual(len(M.history("Test Person", 10)), 3)

        # fuzzy resolve: display name -> the sanitized folder
        self.assertEqual(M.resolve("test person"), "Test_Person")
        self.assertIsNone(M.resolve("nobody at all"))

    def test_junk_filtered_before_limit(self) -> None:
        """Junk must be dropped BEFORE the limit slice, or a contact whose recent
        history is all [reel] yields nothing usable."""
        from shadow import memory as M

        folder = Path(M.PEOPLE_DIR) / "Junky"
        folder.mkdir()
        rows = [{"sender": "them", "content": "real one"}]
        rows += [{"sender": "me", "content": "[reel]"} for _ in range(30)]
        rows += [{"sender": "them", "content": "real two"}]
        (folder / "chat_log.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        history = M.history("Junky", 5)
        self.assertEqual([m.content for m in history], ["real one", "real two"],
                         f"got {[m.content for m in history]}")

    def test_prompt_separates_past_from_live(self) -> None:
        """Stored history and the current screen must be labelled separately, or
        the model answers whatever text sits nearest the end of the blob."""
        from shadow.brain import _user
        from shadow.memory import Msg

        past = [Msg("me", "OLD_LINE")]
        live = [Msg("them", "LIVE_LINE")]
        prompt = _user("Test Friend", past, live)

        self.assertIn("OLD_LINE", prompt)
        self.assertIn("LIVE_LINE", prompt)
        self.assertIn("do NOT reply", prompt)
        self.assertLess(prompt.index("OLD_LINE"), prompt.index("LIVE_LINE"))
        self.assertIn("On screen right now", prompt)

        # no history at all: no context heading, no crash
        prompt2 = _user("Test Friend", [], live)
        self.assertNotIn("do NOT reply", prompt2)
        self.assertIn("LIVE_LINE", prompt2)


class LegacySourceTextTest(unittest.TestCase):
    """legacy source-text checks; replace with behavioural tests in P02/P03"""

    def test_claude_path_has_no_sampling_params(self) -> None:
        """The Claude path 400s on temperature/top_p/top_k. The other provider
        accepts temperature, so only the Claude function is checked. Matches the
        parameter forms, not the bare word — the function's own warning comment
        names them."""
        src = source("brain.py")
        start = src.index("def _reply_claude")
        claude = src[start:src.index("def reply(", start)]
        for param in ("temperature", "top_p", "top_k"):
            for form in (f"{param}=", f'"{param}"', f"'{param}'"):
                self.assertNotIn(form, claude, f"{form} in the Claude path would 400")

    def test_provider_dispatch(self) -> None:
        """Every provider name must be handled, and an unknown one must raise."""
        src = source("brain.py")
        for provider in ("nemotron", "bedrock", "anthropic", "foundry"):
            self.assertIn(f'"{provider}"', src, f"provider {provider} not dispatched")

    def test_goto_and_send_are_guarded(self) -> None:
        """Both the navigation path and the typing path must check the host.
        Typing a DM into a page we did not choose is the failure that matters."""
        src = source("browser.py")
        goto = src[src.index("async def _goto"):src.index("# --- inbox")]
        self.assertIn("check_url(", goto, "_goto does not validate the URL")
        self.assertIn("_assert_on_site()", goto, "_goto does not re-check after redirect")
        send = src[src.index("async def send("):src.index("async def send_burst")]
        self.assertIn("_assert_on_site()", send, "send() does not verify it is on-site")

    def test_unread_heuristic_excludes_own_activity(self) -> None:
        """Previews with no 'You:' prefix are not automatically inbound.

        Counting owner activity as inbound meant opening each thread and calling
        the model before the sender guard rejected it — wasted work and tokens."""
        src = source("browser.py")
        js = src[src.index("_JS_INBOX"):src.index("_JS_READ")]
        for marker in ("You sent an attachment", "to your message", "^You:"):
            self.assertIn(marker, js, f"unread rule no longer checks for {marker!r}")

    def test_iframe_navigation_not_blocked(self) -> None:
        """Instagram's auth sync runs in a third-party iframe. Blocking it on
        every page load risks the login itself; only main-frame navigation can
        actually take us off-site."""
        src = source("browser.py")
        self.assertIn("_is_main_frame", src, "guard no longer distinguishes iframes")
        guard = src[src.index("async def guard"):src.index("await self.page.route")]
        self.assertIn("_is_main_frame(req)", guard, "guard does not check the frame")

    def test_sender_memo_is_bounded_in_source(self) -> None:
        """The in-page sender memo must have a size cap."""
        self.assertIn("__shSender.size > ", source("browser.py"), "sender memo is unbounded")

    def test_no_vision_or_cdp_fallback(self) -> None:
        """Both fallbacks are why the old tree silently failed. Keep them out."""
        src = source("browser.py")
        self.assertNotIn("launch_persistent_context", src, "CDP fallback re-added")
        self.assertNotIn("screenshot", src.lower(), "vision path re-added")


if __name__ == "__main__":
    unittest.main()

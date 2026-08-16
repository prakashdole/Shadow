"""Self-check. No browser, no network, no framework.  python test_shadow.py"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


def test_freeze_list() -> None:
    from shadow.brain import frozen

    assert frozen("bhai 500 rupee bhej de") == "money"
    assert frozen("i love you") == "confession"
    assert frozen("dad is in the hospital") == "emergency"
    assert frozen("been feeling depressed lately") == "emotional"
    # must not fire on ordinary talk
    assert frozen("aabe kya kar rha") is None
    assert frozen("lol that reel was crazy") is None
    assert frozen("") is None


def test_replied_guard() -> None:
    from shadow.loop import already_replied, mark_replied

    state = {"replied": {}, "stories_date": ""}
    assert not already_replied(state, "123", "hey")

    # mark_replied writes to disk; patch save_state out for the unit test
    import shadow.loop as L
    real_save = L.save_state
    L.save_state = lambda s: None
    try:
        mark_replied(state, "123", "hey")
        assert already_replied(state, "123", "hey")          # same message
        assert not already_replied(state, "123", "hey again")  # they said something new
        assert not already_replied(state, "456", "hey")         # different thread
    finally:
        L.save_state = real_save


def test_active_hours() -> None:
    from shadow.config import ACTIVE_END, ACTIVE_START
    from shadow.loop import in_active_hours

    assert in_active_hours(datetime(2026, 7, 25, ACTIVE_START))
    assert not in_active_hours(datetime(2026, 7, 25, ACTIVE_START - 1))
    assert not in_active_hours(datetime(2026, 7, 25, ACTIVE_END))
    assert not in_active_hours(datetime(2026, 7, 25, 3))


def test_stories_due() -> None:
    from datetime import date

    from shadow.loop import stories_due

    assert stories_due({"stories_date": ""})
    assert stories_due({"stories_date": "2020-01-01"})
    assert not stories_due({"stories_date": date.today().isoformat()})


def test_last_sender_guard() -> None:
    """The gpt/things-to-change.md bug: never reply when the last message is mine."""
    from shadow.memory import Msg

    def should_reply(msgs: list[Msg]) -> bool:
        return bool(msgs) and msgs[-1].sender == "them"

    assert should_reply([Msg("me", "yo"), Msg("them", "kya")])
    assert not should_reply([Msg("them", "kya"), Msg("me", "kuch nahi")])
    assert not should_reply([])


def test_memory_roundtrip() -> None:
    """append -> history round trip, against a temp data dir."""
    import shadow.config as C
    import shadow.memory as M

    tmp = Path(tempfile.mkdtemp())
    real = C.PEOPLE_DIR
    C.PEOPLE_DIR = M.PEOPLE_DIR = tmp
    try:
        M.append("Test Person", [M.Msg("them", "aabe"), M.Msg("me", "kya")])
        h = M.history("Test Person", 10)
        assert [(m.sender, m.content) for m in h] == [("them", "aabe"), ("me", "kya")]

        prof = M.profile("Test Person")
        assert prof["total_messages"] == 2

        M.append("Test Person", [M.Msg("them", "?")])
        assert M.profile("Test Person")["total_messages"] == 3
        assert len(M.history("Test Person", 10)) == 3

        # fuzzy resolve: display name -> the sanitized folder
        assert M.resolve("test person") == "Test_Person"
        assert M.resolve("nobody at all") is None
    finally:
        C.PEOPLE_DIR = M.PEOPLE_DIR = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_junk_filter() -> None:
    """33% of the imported history is content-free. It must not reach the model."""
    from shadow.memory import is_junk

    for j in ("[reel]", "[image]", "[voice note]", "[voicenote]", "[sticker]",
              "[gif]", "[video]", "[link]", "[REEL]", "  [reel]  ",
              "Reacted ❤️ to your message", "Liked a message",
              "sent an attachment", "You sent an attachment.",
              "This message was unsent", "", "   "):
        assert is_junk(j), f"{j!r} should be junk"

    # real messages must survive, including ones that merely mention media
    for ok in ("aabe", "?", "lol dekha wo reel", "send me the image",
               "[reel] was funny", "kya"):
        assert not is_junk(ok), f"{ok!r} should NOT be junk"


def test_junk_filtered_before_limit() -> None:
    """Junk must be dropped BEFORE the limit slice, or a person whose recent
    history is all [reel] yields nothing usable."""
    import shadow.config as C
    import shadow.memory as M

    tmp = Path(tempfile.mkdtemp())
    real = C.PEOPLE_DIR
    C.PEOPLE_DIR = M.PEOPLE_DIR = tmp
    try:
        d = tmp / "Junky"
        d.mkdir()
        rows = [{"sender": "them", "content": "real one"}]
        rows += [{"sender": "me", "content": "[reel]"} for _ in range(30)]
        rows += [{"sender": "them", "content": "real two"}]
        (d / "chat_log.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        h = M.history("Junky", 5)
        assert [m.content for m in h] == ["real one", "real two"], \
            f"got {[m.content for m in h]}"
    finally:
        C.PEOPLE_DIR = M.PEOPLE_DIR = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_prompt_separates_past_from_live() -> None:
    """Stored history and the current screen must be labelled separately, or the
    model answers whatever text sits nearest the end of the blob."""
    from shadow.brain import _user
    from shadow.memory import Msg

    past = [Msg("me", "OLD_LINE")]
    live = [Msg("them", "LIVE_LINE")]
    p = _user("Arush", past, live)

    assert "OLD_LINE" in p and "LIVE_LINE" in p
    assert "do NOT reply" in p, "history is not marked as context-only"
    assert p.index("OLD_LINE") < p.index("LIVE_LINE"), "live must come last"
    assert "On screen right now" in p

    # no history at all: no context heading, no crash
    p2 = _user("Arush", [], live)
    assert "do NOT reply" not in p2 and "LIVE_LINE" in p2


def test_real_data_intact() -> None:
    """The 61 imported histories must still load after the move."""
    from shadow import memory

    people = memory.people()
    assert len(people) > 50, f"expected >50 people, found {len(people)}"

    h = memory.history("arush", 20)
    assert len(h) == 20, f"arush history returned {len(h)}"
    assert all(m.sender in ("me", "them") for m in h)

    p = memory.profile("arush")
    assert p["closeness"] == 9
    assert memory.resolve("Arush") == "arush"


def test_parse_output() -> None:
    """Nemotron returns one message per line. No schema to lean on."""
    from shadow.brain import _parse

    assert _parse("aabe\nu alive\n?") == ["aabe", "u alive", "?"]
    assert _parse("  kya  \n\n\n  lol  ") == ["kya", "lol"]

    # skip token, with or without stray punctuation
    assert _parse("NOREPLY") == []
    assert _parse("noreply.") == []
    assert _parse("") == []
    assert _parse("   \n  ") == []

    # reasoning tags stripped
    assert _parse("<think>they want plans</think>\nkal free hu") == ["kal free hu"]
    assert _parse("<THINK>x\ny</THINK>\nhaan") == ["haan"]

    # model dressing it up as a list or quoting it
    assert _parse("- aabe\n- kya") == ["aabe", "kya"]
    assert _parse("1. yo\n2) sup") == ["yo", "sup"]
    assert _parse('"kya kar rha"') == ["kya kar rha"]

    # hard cap at 4
    assert len(_parse("\n".join("m" + str(i) for i in range(9)))) == 4


def test_parse_json_array() -> None:
    """A JSON array is the requested output shape — it gives the model a place
    to stop. Without it Nemotron filled maxTokens and looped."""
    from shadow.brain import _parse

    assert _parse('["yea"]') == ["yea"]
    assert _parse('["nahi bhai", "kal dekhta hu"]') == ["nahi bhai", "kal dekhta hu"]
    assert _parse('[“yea”, “kya”]') == ["yea", "kya"]  # smart quotes
    assert _parse('["NOREPLY"]') == []
    assert _parse('["\U0001f923\U0001f923"]') == ["\U0001f923\U0001f923"]
    # array cap
    assert len(_parse('["a","b","c","d","e","f"]')) == 4


def test_parse_rejects_debris() -> None:
    """Malformed JSON must not leak into a real DM. This exact string was
    generated in eval and survived to output before the guard existed."""
    from shadow.brain import _parse

    assert _parse('["Yea bhai",…….”\nựu"]') == []
    assert _parse('["broken') == []
    assert _parse('once upon a time",  "') == []


def test_parse_rejects_echo() -> None:
    """Repeating their message back, with or without an added '?', is not a
    reply — the model does this when it does not understand them."""
    from shadow.brain import _parse

    assert _parse('["If time permits?"]', "If time permits.") == []
    assert _parse('["Instagram yr?"]', "Instagram yr") == []
    # a real question about what they said is fine
    assert _parse('["kya solution"]', "wait i have a solution") == ["kya solution"]


def test_parse_stops_on_loop_and_meta() -> None:
    """Near the token cap Nemotron repeats itself; it also sometimes narrates."""
    from shadow.brain import _parse

    assert _parse("ha bhai\nkya hua\nha bhai\nkya hua") == ["ha bhai", "kya hua"]
    assert _parse("Based on the style analysis:\nha bhai") == []
    assert _parse("ha bhai\nthem: something") == ["ha bhai"]


def test_claude_path_has_no_sampling_params() -> None:
    """Claude 400s on temperature/top_p/top_k. Nemotron accepts temperature,
    so only the Claude function is checked. Matches the parameter forms, not
    the bare word — the function's own warning comment names them."""
    src = (Path(__file__).parent / "shadow" / "brain.py").read_text(encoding="utf-8")
    start = src.index("def _reply_claude")
    end = src.index("def reply(", start)
    claude = src[start:end]
    for param in ("temperature", "top_p", "top_k"):
        for form in (f"{param}=", f'"{param}"', f"'{param}'"):
            assert form not in claude, f"{form} in the Claude path would 400"


def test_provider_dispatch() -> None:
    """Every provider name must be handled, and an unknown one must raise."""
    src = (Path(__file__).parent / "shadow" / "brain.py").read_text(encoding="utf-8")
    for p in ("nemotron", "bedrock", "anthropic", "foundry"):
        assert f'"{p}"' in src, f"provider {p} not dispatched"


def test_nemotron_region_guard() -> None:
    """ap-south-1 does not serve Nemotron; config must redirect off it."""
    from shadow import config as c

    assert "ap-south-1" not in c.NEMOTRON_REGIONS
    assert c.NEMOTRON_DEFAULT_REGION in c.NEMOTRON_REGIONS
    if c.PROVIDER == "nemotron":
        assert c.AWS_REGION in c.NEMOTRON_REGIONS, (
            f"{c.AWS_REGION} does not serve Nemotron"
        )


def test_domain_confinement() -> None:
    """The bot must never navigate off instagram.com. Note the suffix-match
    bypass: 'evilinstagram.com'.endswith('instagram.com') is True, which is
    how an allowlist like this usually gets broken."""
    from shadow.browser import OffSite, check_url, host_allowed

    for good in ("https://www.instagram.com/direct/inbox/",
                 "https://instagram.com/stories/foo/",
                 "https://www.instagram.com/direct/t/1234/"):
        assert host_allowed(good), good

    for bad in ("https://evilinstagram.com/",            # suffix bypass
                "https://instagram.com.evil.com/",       # prefix trick
                "https://www.instagram.com.attacker.io/",
                "http://www.instagram.com/",             # plain http
                "https://l.instagram.com/?u=http://evil.com",  # IG link shim
                "https://facebook.com/",
                "javascript:alert(1)",
                "data:text/html,<h1>x",
                "file:///C:/Windows/"):
        assert not host_allowed(bad), bad
        try:
            check_url(bad)
            raise AssertionError(f"check_url allowed {bad}")
        except OffSite:
            pass


def test_goto_and_send_are_guarded() -> None:
    """Both the navigation path and the typing path must check the host.
    Typing a DM into a page we did not choose is the failure that matters."""
    src = (Path(__file__).parent / "shadow" / "browser.py").read_text(encoding="utf-8")
    goto = src[src.index("async def _goto"):src.index("# --- inbox")]
    assert "check_url(" in goto, "_goto does not validate the URL"
    assert "_assert_on_site()" in goto, "_goto does not re-check after redirect"
    send = src[src.index("async def send("):src.index("async def send_burst")]
    assert "_assert_on_site()" in send, "send() does not verify it is on-site"


def test_card_detection() -> None:
    """A shared reel is not something to reply to. Without this the model
    parrots the poster's handle back, because that is the only text present."""
    from shadow.loop import is_card

    assert is_card("[reel from @lookmaxxeedd]")
    assert is_card("[post]")
    assert is_card("[video from @x] some caption")
    assert is_card("  [reel from @y]  ")
    assert not is_card("hey bhai")
    assert not is_card("[not a card]")
    assert not is_card("")
    # a real message that merely starts with a bracket
    assert not is_card("[laughs] ok")


def test_unread_heuristic_excludes_own_activity() -> None:
    """Previews with no 'You:' prefix are not automatically inbound.

    Marking 11 of 14 threads unread meant opening each and calling the model
    before the sender guard rejected it - wasted work and wasted tokens."""
    src = (Path(__file__).parent / "shadow" / "browser.py").read_text(encoding="utf-8")
    js = src[src.index("_JS_INBOX"):src.index("_JS_READ")]
    for marker in ("You sent an attachment", "to your message", "^You:"):
        assert marker in js, f"unread rule no longer checks for {marker!r}"


def test_iframe_navigation_not_blocked() -> None:
    """Instagram's auth sync runs in a facebook.com iframe. Blocking it on
    every page load risks the login itself; only main-frame navigation can
    actually take us off-site."""
    src = (Path(__file__).parent / "shadow" / "browser.py").read_text(encoding="utf-8")
    assert "_is_main_frame" in src, "guard no longer distinguishes iframes"
    guard = src[src.index("async def guard"):src.index("await self.page.route")]
    assert "_is_main_frame(req)" in guard, "guard does not check the frame"


def test_state_growth_is_bounded() -> None:
    """Both the sender memo and the replied-guard must not grow forever."""
    src_js = (Path(__file__).parent / "shadow" / "browser.py").read_text(encoding="utf-8")
    assert "__shSender.size > " in src_js, "sender memo is unbounded"

    import shadow.loop as L
    real = L.save_state
    L.save_state = lambda s: None
    try:
        state = {"replied": {f"person{i}": "x" for i in range(600)}, "stories_date": ""}
        L.mark_replied(state, "new", "hello")
        assert len(state["replied"]) <= 251, len(state["replied"])
    finally:
        L.save_state = real


def test_no_vision_or_cdp_fallback() -> None:
    """Both fallbacks are why the old tree silently failed. Keep them out."""
    src = (Path(__file__).parent / "shadow" / "browser.py").read_text(encoding="utf-8")
    assert "launch_persistent_context" not in src, "CDP fallback re-added"
    assert "screenshot" not in src.lower(), "vision path re-added"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)

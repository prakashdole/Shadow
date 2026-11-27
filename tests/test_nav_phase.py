"""Phase 1 navigation: ordering, frozen split, and one browser pass."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shadow.nav_phase import _demo, choose_oldest, list_order
from tools.nav_phase_benchmark import eventual, first_attempt, required_successes


ROOT = Path(__file__).resolve().parents[1]


class OrderTests(TestCase):
    def test_self_check_and_thresholds(self):
        _demo()
        self.assertEqual(required_successes(60), 57)
        self.assertEqual(required_successes(40), 38)
        self.assertEqual(required_successes(20), 19)
        self.assertIsNone(list_order("Conversations"))

    def test_missing_time_is_not_a_tie(self):
        class Row:
            def __init__(self, name, y):
                self.name, self.center = name, (0, y)

        self.assertIsNone(choose_oldest([Row("Asha", 10), Row("Bela 2h", 40)], "newest"))

    def test_eventual_allows_one_closed_photo_and_rejects_a_wrong_thread(self):
        case = {"expect": {"outcome": "acted", "opened": ["reel"], "state": "reel"}}
        self.assertTrue(eventual(case, "acted", ["photo", "close-photo", "reel"], "reel"))
        self.assertFalse(first_attempt(case, "acted", ["photo", "close-photo", "reel"], "reel",
                                       wrong_clicks=1, second_look=False, recovered=True))
        held = {"expect": {"outcome": "hold", "opened": []}}
        self.assertTrue(eventual(held, "hold", [], ""))
        self.assertFalse(eventual(held, "acted", ["11001"], "11001"))

    def test_frozen_split_excludes_known_misses(self):
        frozen = json.loads((ROOT / "tests/fixtures/nav_phase_frozen.json").read_text())
        dev = json.loads((ROOT / "tests/fixtures/nav_phase_dev.json").read_text())
        phase = [case for case in frozen["cases"] if case["split"] == "phase1"]
        reserve = [case for case in frozen["cases"] if case["split"] == "reserve"]
        self.assertEqual(len(phase), 60)
        self.assertEqual(len(reserve), 60)
        self.assertEqual(sum(case["kind"] in ("act", "journey") for case in phase), 40)
        self.assertEqual(sum(case["kind"] == "journey" for case in phase), 20)
        self.assertEqual(frozen["frozen"]["provider_call_budget"], 640)
        self.assertEqual(frozen["frozen"]["playwright"], "excluded")
        held = json.loads((ROOT / "tests/fixtures/nav_heldout.json").read_text())["cases"]
        known = {case["id"]: case for case in held if case["id"] in dev["known_sources"]}
        self.assertEqual(len(known), 10)
        self.assertEqual(sum(case["family"] == "lowest" for case in known.values()), 6)
        self.assertEqual(sum(case["family"] == "reel" for case in known.values()), 4)
        claim_ids = {case["id"] for case in phase + reserve}
        self.assertTrue(claim_ids.isdisjoint(dev["known_sources"]))
        source = (ROOT / "shadow/nav_phase.py").read_text().split("def _demo", 1)[0]
        for hook in ("shadowLocate", "shadowToken", "shadowOutcome", "shadowProbe", "shadowMutate", "playwright"):
            self.assertNotIn(hook, source)

    def test_agent_nav_uses_the_normal_entry_point(self):
        from tools.screenshot_turn import main

        with TemporaryDirectory() as directory, patch("tools.screenshot_turn.chromium") as browser, \
                patch("shadow.agent_session.AgentSession") as session, \
                patch("tools.screenshot_turn.open_storage") as storage, \
                patch("tools.screenshot_turn.gemini_keys", return_value=["key"]), \
                patch("tools.screenshot_turn.openai_api_key", return_value="key"), \
                patch("tools.screenshot_turn.run_turn", return_value={"outcome": "hold", "sends": 0, "reactions": 0}) as turn:
            session.return_value.__enter__.return_value.navigate = lambda _url: None
            storage.return_value.__enter__.return_value.get_runtime.return_value = {}
            main(["--data-dir", directory, "--fixture", "http://127.0.0.1/direct/inbox/",
                  "--observe", "agent-nav"])
        self.assertEqual(turn.call_args.kwargs["observe"], "agent-nav")
        self.assertTrue(session.called)
        self.assertFalse(browser.called)


if __name__ == "__main__":
    _demo()

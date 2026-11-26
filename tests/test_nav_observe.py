"""Regression checks for the opt-in navigation pilot."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shadow.nav_observe import _demo, select_target
from shadow.screen_decision import choose_action, decision_instructions
from shadow.screenshot_loop import TASK_ROW
from shadow.vision_prompts import DECISION_ROLE


ROOT = Path(__file__).resolve().parents[1]


class PolicyTests(TestCase):
    def test_self_check(self):
        _demo()

    def test_evidence_is_omitted_unless_a_snapshot_was_aligned(self):
        self.assertEqual(decision_instructions(TASK_ROW), DECISION_ROLE + "\nTask: " + TASK_ROW)
        self.assertIn("Untrusted labels", decision_instructions(TASK_ROW, "c1: Asha unread"))

    def test_choice_boundary_sees_the_png_that_will_be_sent(self):
        seen = {}

        def transport(payload):
            return {"answers": {"action": {"choice": "hold", "confidence": 0.95}}}

        def boundary(event):
            seen["image"] = event["image"]

        image = b"\x89PNG\r\n\x1a\n" + b"pixels"
        choose_action("offline", image, TASK_ROW, {"hold": "Hold.", "wait": "Wait."},
                      transport=transport, boundary=boundary)
        self.assertEqual(seen["image"], image)

    def test_held_out_split_is_frozen(self):
        document = json.loads((ROOT / "tests/fixtures/nav_heldout.json").read_text())
        cases = document["cases"]
        self.assertEqual(len(cases), 120)
        self.assertGreaterEqual(sum(case["kind"] in ("act", "journey", "race") for case in cases), 60)
        self.assertGreaterEqual(sum(case["kind"] in ("hold", "wait") for case in cases), 40)
        self.assertEqual(sum(case["live"] for case in cases), 60)
        self.assertEqual(document["frozen"]["agent_browser"], "0.45.0")
        self.assertEqual(document["frozen"]["provider_call_budget"], 280)
        ids = [case["id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        for case in cases:
            self.assertNotIn("expect", case["screen"])
            self.assertNotIn("inject", case["screen"])

    def test_lowest_unread_row_wins_when_several_are_marked(self):
        from shadow.nav_observe import Bound
        low = Bound("c1", "row-1", "row", (0, 0, 10, 10), (5, 5), "Inbox row 1.", "1", "")
        high = Bound("c2", "row-2", "row", (0, 40, 10, 50), (5, 45), "Inbox row 2. A visible unread dot marks this row.", "2", "")
        other = Bound("c3", "row-3", "row", (0, 80, 10, 90), (5, 85), "Inbox row 3. A visible unread dot marks this row.", "3", "")
        self.assertEqual(select_target(TASK_ROW, [low, high, other]).choice, "c3")

    def test_observe_flag_reaches_the_screenshot_entry_point(self):
        from tools.screenshot_turn import main

        with TemporaryDirectory() as directory, patch("tools.screenshot_turn.chromium") as browser, \
                patch("tools.screenshot_turn.open_storage") as storage, \
                patch("tools.screenshot_turn.gemini_keys", return_value=["key"]), \
                patch("tools.screenshot_turn.openai_api_key", return_value="key"), \
                patch("tools.screenshot_turn.run_turn", return_value={"outcome": "hold", "sends": 0, "reactions": 0}) as turn:
            browser.return_value.__enter__.return_value.navigate = lambda _url: None
            storage.return_value.__enter__.return_value.get_runtime.return_value = {}
            main(["--data-dir", directory, "--fixture", "http://127.0.0.1/direct/inbox/",
                  "--observe", "agent-browser"])
        self.assertEqual(turn.call_args.kwargs["observe"], "agent-browser")


if __name__ == "__main__":
    _demo()

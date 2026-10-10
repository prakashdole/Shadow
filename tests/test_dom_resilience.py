"""Pure regression guards for conservative control-boundary interpretation."""

from __future__ import annotations

import unittest

from shadow.dom import interpret_read


class ControlTextBoundaryInterpretationTest(unittest.TestCase):
    def test_only_explicit_reaction_evidence_attaches_reaction_metadata(self):
        raw = {
            "path": "/direct/t/123/", "pane_present": True,
            "pane": {"left": 0.0, "right": 1000.0, "width": 1000.0},
            "header": {"profile_links": 1, "avatars": 1},
            "articles": [{"left": 100.0, "width": 300.0, "centre": 250.0,
                          "laid": True, "text": "message", "media_label": None,
                          "reply_to_text": None, "edited": False}],
            "article_orders": [0],
            "outside": [{"order": 1, "left": 0.0, "width": 24.0,
                         "centre": 12.0, "laid": True, "text": "👍",
                         "reaction": True, "target_order": 0}],
            "seen": False,
        }
        result = interpret_read(raw, "123")
        self.assertEqual([event.kind for event in result.events], ["text", "reaction"])
        self.assertEqual(result.events[0].evidence["reactions"], ["👍"])


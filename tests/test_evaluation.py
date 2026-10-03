"""Causal partitions and Wilson metrics, no paid generation."""

import asyncio
import unittest

from fake_ports import FakeDecisions
from shadow.evaluation import RATING_DIMENSIONS, blind_pairs, evaluate, rating_report, validate_dataset, wilson, decision_input


def fictional_case():
    return {"id": "fictional-1", "episode_id": "episode-a", "split": "dev", "contact_id": 1,
            "cutoff": 2, "expected": "reply", "category": "ordinary", "authorship": "model_drafted",
            "events": [{"id": 1, "time": 1, "sender": "contact", "kind": "text", "text": "hello"}],
            "current_ids": [1]}


class EvaluationTests(unittest.TestCase):
    def test_split_and_future_source_leakage_rejected(self):
        a, b = fictional_case(), fictional_case()
        b.update(id="fictional-2", split="test")
        with self.assertRaises(ValueError):
            validate_dataset([a, b])
        a["events"][0]["time"] = 3
        with self.assertRaises(ValueError):
            validate_dataset([a])

    def test_cross_contact_and_heldout_derived_memory_rejected(self):
        case = fictional_case()
        case["facts"] = [{"contact_id": 2, "time": 1, "split": "train", "episode_id": "other", "source_ids": [1]}]
        with self.assertRaises(ValueError):
            validate_dataset([case])

    def test_evaluation_passes_validated_retrieval_to_decisions(self):
        case = fictional_case()
        case["facts"] = [{"id": 8, "value_text": "fictional tea preference", "contact_id": 1,
                          "time": 1, "split": "train", "episode_id": "training", "source_ids": [1]}]
        validate_dataset([case])
        self.assertEqual(decision_input(case).facts[0]["id"], 8)
        case["current_ids"] = [1, 1]
        with self.assertRaises(ValueError):
            validate_dataset([case])

    def test_metrics_and_external_gates_remain_explicit(self):
        self.assertIsNone(wilson(0, 0)["rate"])
        self.assertLess(wilson(1, 1)["interval95"][0], 0.5)
        report = asyncio.run(evaluate([fictional_case()], FakeDecisions()))
        self.assertEqual(report["decisions"]["total"], 1)
        self.assertEqual(report["owner_written_style_cases"], 0)
        self.assertEqual(report["qualification"], "pending_external")

    def test_blind_ratings_keep_key_separate_and_report_small_samples(self):
        visible, key = blind_pairs([{"id": "one", "context": "fictional context",
                                    "personalized": "hello", "baseline": "hi"}], seed=7)
        self.assertNotIn("personalized", visible[0])
        side = "a" if key[0]["a"] == "personalized" else "b"
        row = {"id": "one", "preferred": side,
               "scores": {letter: dict.fromkeys(RATING_DIMENSIONS, 4) for letter in ("a", "b")}}
        report = rating_report([row], key)
        self.assertEqual(report["preferred"], 1)
        self.assertFalse(report["minimum_owner_sample_met"])
        with self.assertRaises(ValueError):
            rating_report([row, row], key)


if __name__ == "__main__":
    unittest.main()

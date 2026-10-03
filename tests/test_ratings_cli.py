"""Private rating artifacts; incomplete forms never become owner evidence."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from click.testing import CliRunner

from shadow.main import cli
from shadow.evaluation import RATING_DIMENSIONS


class RatingsCLI(unittest.TestCase):
    def test_prepare_report_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as scratch:
            os.chmod(scratch, 0o700)
            pairs = Path(scratch) / "input.json"
            pairs.write_text(json.dumps([{"id": "fictional", "context": "A greeting",
                                         "personalized": "hey", "baseline": "hello"}]))
            target = Path(scratch) / "rating"
            runner = CliRunner()
            result = runner.invoke(cli, ["ratings", "prepare", "--pairs", str(pairs),
                "--output-dir", str(target), "--seed", "7"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("personalized", (target / "pairs.json").read_text())
            self.assertEqual((target / "answer-key.json").stat().st_mode & 0o777, 0o600)
            args = ["ratings", "report", "--ratings", str(target / "ratings.json"),
                    "--key", str(target / "answer-key.json"), "--output", str(target / "report.json")]
            self.assertNotEqual(runner.invoke(cli, args).exit_code, 0)
            filled = [{"id": "fictional", "preferred": "tie", "scores": {
                side: dict.fromkeys(RATING_DIMENSIONS, 4) for side in ("a", "b")}}]
            (target / "ratings.json").write_text(json.dumps(filled))
            self.assertEqual(runner.invoke(cli, args).exit_code, 0)
            self.assertNotEqual(runner.invoke(cli, args).exit_code, 0)
            self.assertEqual(json.loads((target / "report.json").read_text())["qualification"], "pending_external")

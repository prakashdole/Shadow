"""tests/test_privacy_audit.py — A01/A02 evidence for the privacy audit tool.

The canary secret is assembled at runtime so this file never contains a
secret-looking literal, and the tool's own output is asserted never to contain
it. Run: python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "privacy_audit.py"

CANARY_NAME = "AK" + "IA" + "Q" * 16


class PrivacyAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="privacy-audit-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOL), *args],
            capture_output=True, text=True, cwd=str(cwd or REPO),
        )

    def test_tool_source_does_not_match_its_own_patterns(self) -> None:
        proc = self.run_tool("--tree", str(TOOL))
        self.assertEqual(
            proc.returncode, 0,
            f"the audit flags its own source:\n{proc.stdout}{proc.stderr}",
        )

    def test_seeded_secret_is_reported_without_printing_it(self) -> None:
        target = self.tmp / "canary.py"
        target.write_text(f'KEY = "{CANARY_NAME}"\n', encoding="utf-8")
        proc = self.run_tool("--tree", str(self.tmp))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"secret/aws-access-key-id\t{self._rel(target)}", proc.stdout)
        self.assertNotIn(CANARY_NAME, proc.stdout)
        self.assertNotIn(CANARY_NAME, proc.stderr)
        self.assertIn("SUMMARY\tsecret/aws-access-key-id\t1", proc.stdout)
        self.assertIn("SUMMARY\ttotal\t1", proc.stdout)

    def test_summary_mode_prints_counts_only(self) -> None:
        (self.tmp / "canary.py").write_text(CANARY_NAME, encoding="utf-8")
        proc = self.run_tool("--tree", str(self.tmp), "--summary")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn(self._rel(self.tmp / "canary.py"), proc.stdout)
        self.assertNotIn(CANARY_NAME, proc.stdout)
        self.assertIn("SUMMARY\tsecret/aws-access-key-id\t1", proc.stdout)

    def test_private_corpus_path_is_reported(self) -> None:
        chat = self.tmp / "data" / "people" / "Fictional" / "chat_log.jsonl"
        chat.parent.mkdir(parents=True)
        chat.write_text('{"sender": "them", "content": "hello"}\n', encoding="utf-8")
        proc = self.run_tool("--tree", str(self.tmp))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("corpus\tdata/people/Fictional/chat_log.jsonl", proc.stdout)

    def test_clean_tree_passes(self) -> None:
        (self.tmp / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        fixture = self.tmp / "fixtures" / "sample.jsonl"
        fixture.parent.mkdir()
        fixture.write_text('{"sender": "them", "content": "fictional"}\n', encoding="utf-8")
        proc = self.run_tool("--tree", str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SUMMARY\ttotal\t0", proc.stdout)

    def test_missing_tree_is_a_usage_error(self) -> None:
        proc = self.run_tool("--tree", str(self.tmp / "nope"))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_tracked_file_that_is_also_ignored_is_reported(self) -> None:
        repo = self._init_repo()
        (repo / ".gitignore").write_text("/cookies.json\n", encoding="utf-8")
        (repo / "cookies.json").write_text("{}\n", encoding="utf-8")
        self._commit(repo, add=["-f", ".gitignore", "cookies.json"])
        proc = self.run_tool(cwd=repo)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("tracked-ignored\tcookies.json", proc.stdout)
        self.assertIn("credentials\tcookies.json", proc.stdout)

    def test_history_reports_a_committed_then_deleted_private_path(self) -> None:
        repo = self._init_repo()
        corpus = repo / "data" / "people" / "Fictional" / "chat_log.jsonl"
        corpus.parent.mkdir(parents=True)
        corpus.write_text('{"sender": "them", "content": "gone"}\n', encoding="utf-8")
        self._commit(repo, add=["data/people/Fictional/chat_log.jsonl"])
        corpus.unlink()
        self._commit(repo, add=["-A"])

        live = self.run_tool(cwd=repo)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)

        proc = self.run_tool("--history", cwd=repo)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("corpus\tdata/people/Fictional/chat_log.jsonl", proc.stdout)

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.tmp))

    def _init_repo(self) -> Path:
        self.assertEqual(
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.tmp).returncode, 0
        )
        return self.tmp

    def _commit(self, repo: Path, add: list[str]) -> None:
        # Identity and signing are per-invocation; the global config is never touched.
        base = ["git", "-c", "user.name=t", "-c", "user.email=t@t",
                "-c", "commit.gpgsign=false"]
        self.assertEqual(subprocess.run(base + ["add", *add], cwd=repo).returncode, 0)
        self.assertEqual(
            subprocess.run(base + ["commit", "-qm", "fixture"], cwd=repo).returncode, 0
        )


if __name__ == "__main__":
    unittest.main()

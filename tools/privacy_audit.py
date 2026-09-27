#!/usr/bin/env python3
"""Read-only privacy audit for a Shadow checkout.

Reports CATEGORY<TAB>path for every private path or secret pattern it finds,
then a per-category count summary. It never prints file contents, never prints
the text a secret pattern matched, and never modifies anything.

Exit codes: 0 clean, 1 findings, 2 usage error.

  python3 tools/privacy_audit.py                    # audit the repo in cwd
  python3 tools/privacy_audit.py --summary          # counts only
  python3 tools/privacy_audit.py --history          # include every path ever
                                                    # committed on any ref
  python3 tools/privacy_audit.py --tree DIR         # plain dir, no git needed

The regex sources below are deliberately written so this file cannot match its
own patterns: a pattern is only assembled into a matchable form from separate
string fragments.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

MAX_SCAN_BYTES = 2 * 1024 * 1024

_VOICE_FILES = frozenset({"voice.json", "personality.md", "eval_cases.json"})
_CREDENTIAL_FILES = frozenset({"cookies.json", "storage-state.json"})
_TOOLING_DIRS = (
    "graphify-out/", ".code-review-graph/", ".reticle/", ".obsidian/",
)
_CAPTURE_DIRS = ("drive-shots/", "screenshots/", "traces/", "captures/")
_BROWSER_DIRS = ("browser-profile/", "chrome-profile/")


def _secret_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """(name, regex) pairs. Assembled so this file never matches itself."""
    return [
        ("aws-access-key-id", re.compile("AK" + "IA[0-9A-Z]{16}")),
        ("aws-temp-access-key-id", re.compile("AS" + "IA[0-9A-Z]{16}")),
        (
            "aws-secret-access-key",
            re.compile("aws_secret_access_key" + r"\s*[=:]\s*[A-Za-z0-9/+]{40}"),
        ),
        ("anthropic-key", re.compile("sk-ant" + "-[A-Za-z0-9_-]{20,}")),
        ("abs-key", re.compile("AB" + "SK[A-Za-z0-9+/=]{20,}")),
        (
            "telegram-bot-token",
            re.compile(r"\b\d{8,10}:" + "[A-Za-z0-9_-]{35}\b"),
        ),
        (
            "private-key-block",
            re.compile("-----BEGIN " + "[A-Z ]*" + "PRIVATE KEY" + "-" * 5),
        ),
    ]


SECRET_PATTERNS = _secret_patterns()


def private_categories(path: str) -> list[str]:
    """Every private category a repository-relative path falls into."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    name = p.rsplit("/", 1)[-1]
    hits = []

    if p.startswith("data/"):
        hits.append("corpus")
    if name in _VOICE_FILES:
        hits.append("voice")
    if (
        name == ".shadow_state.json"
        or name.endswith((".log", ".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm"))
        or ".sqlite" in name
    ):
        hits.append("runtime")
    if p.startswith("backups/") or fnmatch.fnmatch(name, "data-backup-*.tar.gz"):
        hits.append("backup")
    if (
        name == ".env"
        or (name.startswith(".env.") and name != ".env.example")
        or p.startswith(".credentials-archive/")
        or name in _CREDENTIAL_FILES
    ):
        hits.append("credentials")
    if p == ".claude/settings.local.json" or name == ".mcp.json" or p.startswith(_TOOLING_DIRS):
        hits.append("tooling")
    if name == "probe.json" or p.startswith(_CAPTURE_DIRS):
        hits.append("captures")
    if p.startswith(_BROWSER_DIRS):
        hits.append("browser")
    return hits


def _git(root: Path, *args: str) -> list[str] | None:
    proc = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotepath=false", *args],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def _scan_secrets(root: Path, paths: list[str], findings: set[tuple[str, str]]) -> None:
    for rel in paths:
        target = root / rel
        try:
            if not target.is_file() or target.stat().st_size > MAX_SCAN_BYTES:
                continue
            blob = target.read_bytes()
        except OSError:
            continue
        if b"\0" in blob:
            continue
        text = blob.decode("utf-8", "replace")
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.add((f"secret/{name}", rel))


def _scan_paths(paths: list[str], findings: set[tuple[str, str]]) -> None:
    for rel in paths:
        for category in private_categories(rel):
            findings.add((category, rel))


def audit_git(root: Path, history: bool) -> set[tuple[str, str]]:
    findings: set[tuple[str, str]] = set()
    tracked = _git(root, "ls-files", "-z")
    if tracked is None:
        raise RuntimeError("not inside a git repository")
    _scan_paths(tracked, findings)

    tracked_ignored = _git(root, "ls-files", "-ci", "--exclude-standard", "-z") or []
    for rel in tracked_ignored:
        findings.add(("tracked-ignored", rel))

    _scan_secrets(root, tracked, findings)

    if history:
        # git log --name-only is newline separated; a filename containing a
        # newline would split, which is the known ceiling of this mode.
        history_paths = _git(root, "log", "--all", "--name-only", "--format=")
        seen = {line for line in (history_paths[0] if history_paths else "").splitlines()
                if line.strip()}
        _scan_paths(sorted(seen), findings)
    return findings


def audit_tree(target: Path) -> set[tuple[str, str]]:
    findings: set[tuple[str, str]] = set()
    if target.is_file():
        paths = [target.name]
    else:
        paths = sorted(
            str(p.relative_to(target)) for p in target.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(target).parts
        )
    _scan_paths(paths, findings)
    _scan_secrets(target, paths, findings)
    return findings


def report(findings: set[tuple[str, str]], summary_only: bool) -> int:
    counts: dict[str, int] = {}
    for category, _ in findings:
        counts[category] = counts.get(category, 0) + 1
    if not summary_only:
        for category, path in sorted(findings):
            print(f"{category}\t{path}")
    for category in sorted(counts):
        print(f"SUMMARY\t{category}\t{counts[category]}")
    print(f"SUMMARY\ttotal\t{len(findings)}")
    return EXIT_FINDINGS if findings else EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only private-artifact and secret scan. Paths only.")
    parser.add_argument("--tree", metavar="DIR", help="scan a plain directory, no git")
    parser.add_argument("--history", action="store_true",
                        help="also report private paths ever committed on any ref")
    parser.add_argument("--summary", action="store_true", help="counts only")
    args = parser.parse_args(argv)

    try:
        if args.tree:
            target = Path(args.tree).resolve()
            if not target.exists():
                print(f"usage error: {args.tree} does not exist", file=sys.stderr)
                return EXIT_USAGE
            findings = audit_tree(target)
        else:
            top = _git(Path.cwd(), "rev-parse", "--show-toplevel")
            root = Path(top[0].strip()) if top and top[0].strip() else None
            if root is None:
                print("usage error: not inside a git repository; use --tree DIR",
                      file=sys.stderr)
                return EXIT_USAGE
            findings = audit_git(root, args.history)
    except RuntimeError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    return report(findings, args.summary)


if __name__ == "__main__":
    raise SystemExit(main())

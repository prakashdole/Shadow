"""Compatibility entry point. The suites live in tests/ - see tests/README.md.

    python3 test_shadow.py                              # offline suite
    python3 -m unittest discover -s tests -p 'test_*.py' -v
    python3 -m unittest discover -s tests/browser -p 'test_*.py' -v

Runs the offline suite and then the browser suite, and exits non-zero if
either fails. No browser, no network, no owner data. Kept so the old command
still works.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent / "tests"
BROWSER_TESTS = TESTS / "browser"


def _run(name: str, directory: Path) -> tuple[int, int]:
    if not directory.is_dir():
        print(f"no test directory at {directory}", file=sys.stderr)
        return 0, 1
    print(f"\n=== {name} ===")
    # top_level_dir is passed explicitly: the loader remembers it from the first
    # discovery, and inheriting it would make the second directory look like a
    # package that is missing its __init__.py.
    loader = unittest.TestLoader()
    suite = loader.discover(str(directory), pattern="test_*.py",
                            top_level_dir=str(directory))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    bad = len(result.failures) + len(result.errors)
    return result.testsRun, bad


def main() -> int:
    offline_total, offline_bad = _run("offline suite", TESTS)
    browser_total, browser_bad = _run("browser suite", BROWSER_TESTS)
    total, bad = offline_total + browser_total, offline_bad + browser_bad
    print(f"\n{total - bad}/{total} passed"
          f"  (offline {offline_total - offline_bad}/{offline_total},"
          f" browser {browser_total - browser_bad}/{browser_total})")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

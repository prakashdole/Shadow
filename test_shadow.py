"""Compatibility entry point. The real suite lives in tests/ — see tests/README.md.

    python3 test_shadow.py                              # same suite
    python3 -m unittest discover -s tests -p 'test_*.py' -v

No browser, no network, no owner data. Kept so the old command still works.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent / "tests"


def main() -> int:
    if not TESTS.is_dir():
        print(f"no test directory at {TESTS}", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.discover(str(TESTS), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print(f"\n{total - bad}/{total} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

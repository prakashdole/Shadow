#!/usr/bin/env python3
"""Private checks against the owner's real corpus. Not part of the public suite.

These are the two legacy self-checks that could not be made fictional: they read
the real data/ tree to prove the imported histories still load after the P01
path move. They print PASS/FAIL and counts only — never a contact name, folder
name, message, handle or value.

    python3 tools/private_checks.py

Requires the private data directory. Never run in CI, never on a clean copy.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from shadow.config import PEOPLE_DIR
    from shadow import memory

    print(f"people_dir_present: {PEOPLE_DIR.exists()}")
    if not PEOPLE_DIR.exists():
        print("RESULT: FAIL (no private corpus at the configured people dir)")
        return 1

    people = memory.people()
    print(f"people_count: {len(people)}")
    print(f"people_count_over_50: {len(people) > 50}")

    # test_no_stray_files_in_people_root: a loose profile.json in the shared root
    # means some name collapsed to the root and its history is unreachable.
    strays = sum(1 for p in PEOPLE_DIR.iterdir() if p.is_file())
    print(f"stray_files_in_people_root: {strays}")

    profile_ok = 0
    profile_bad_keys = 0
    history_ok = 0
    history_20plus = 0
    bad_senders = 0
    resolve_ok = 0

    for name in people:
        try:
            profile = memory.profile(name)
        except Exception:
            continue
        if all(k in profile for k in ("display_name", "relationship", "closeness")):
            profile_ok += 1
        else:
            profile_bad_keys += 1
        if isinstance(profile.get("closeness"), int) and 1 <= profile["closeness"] <= 10:
            resolve_ok += 1
        try:
            history = memory.history(name, 20)
        except Exception:
            history = []
        if history:
            history_ok += 1
        if len(history) >= 20:
            history_20plus += 1
        if any(m.sender not in ("me", "them") for m in history):
            bad_senders += 1

    print(f"profiles_loadable: {profile_ok}/{len(people)}")
    print(f"profiles_missing_keys: {profile_bad_keys}")
    print(f"profiles_with_closeness_in_range: {resolve_ok}/{len(people)}")
    print(f"histories_readable: {history_ok}/{len(people)}")
    print(f"histories_with_20_or_more_usable_messages: {history_20plus}")
    print(f"histories_with_unexpected_sender: {bad_senders}")

    failures = []
    if strays:
        failures.append("stray files loose in people root")
    if not len(people) > 50:
        failures.append("people count not over 50")
    if profile_bad_keys:
        failures.append("profiles missing expected keys")
    if bad_senders:
        failures.append("unexpected sender in stored history")
    if profile_ok != len(people) or resolve_ok != len(people):
        failures.append("not every profile loaded")

    for reason in failures:
        print(f"FAIL: {reason}")
    print(f"RESULT: {'FAIL' if failures else 'PASS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Strip content-free rows from the stored chat logs.

The old importer wrote a placeholder row for every non-text message
(`[reel]`, `Reacted X to your message`, attachment notices). About a third of
the imported history is those rows; they give the model nothing to imitate.

Idempotent — safe to re-run after importing more history.

  python prune_junk.py --dry-run    # report only
  python prune_junk.py              # rewrite the files
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from shadow.config import PEOPLE_DIR
from shadow.memory import is_junk


def main(dry_run: bool) -> None:
    kept_total = dropped_total = 0
    examples: Counter[str] = Counter()
    touched = 0

    for log in sorted(PEOPLE_DIR.glob("*/chat_log.jsonl")):
        keep: list[str] = []
        dropped = 0
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                content = (json.loads(line).get("content") or "")
            except json.JSONDecodeError:
                keep.append(line)   # unparseable: leave it alone, don't destroy
                continue
            if is_junk(content):
                dropped += 1
                examples[content.strip()[:40]] += 1
            else:
                keep.append(line)

        kept_total += len(keep)
        dropped_total += dropped
        if dropped:
            touched += 1
            print(f"  {log.parent.name:28} -{dropped:5}  keeping {len(keep)}")
            if not dry_run:
                log.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
                # keep the counter honest
                prof_path = log.parent / "profile.json"
                if prof_path.exists():
                    try:
                        prof = json.loads(prof_path.read_text(encoding="utf-8"))
                        prof["total_messages"] = len(keep)
                        prof_path.write_text(
                            json.dumps(prof, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    except json.JSONDecodeError:
                        pass

    print(f"\n{'WOULD DROP' if dry_run else 'DROPPED'} {dropped_total} rows "
          f"across {touched} files; {kept_total} remain")
    print("\nmost common junk:")
    for text, n in examples.most_common(8):
        print(f"  {n:5}  {text!r}")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)

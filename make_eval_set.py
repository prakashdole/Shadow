"""Freeze a held-out test set: eval_cases.json.

Why this exists: eval_voice.py used to draw its cases from voice.json's
`examples`, which build_voice.py reshuffles on every rebuild. So each eval run
scored a DIFFERENT test set, and his own reply statistics moved between runs
(for example, the language mix) even though his replies obviously cannot change. Real
gains and sampling noise were indistinguishable.

Fixed set, written once, never regenerated casually:
  - drawn straight from the corpus, not from voice.json
  - build_voice.py excludes these exchanges from the few-shot examples, so a
    test case is never sitting in the prompt with its answer attached
  - stratified across people so one chatty contact cannot dominate

    python make_eval_set.py          # writes eval_cases.json (refuses overwrite)
    python make_eval_set.py --force  # regenerate from scratch
"""

from __future__ import annotations

import json
import random
import sys

from shadow.config import PEOPLE_DIR, ROOT
from shadow.memory import is_junk

OUT = ROOT / "eval_cases.json"
PER_PERSON = 4
SEED = 20260726


def load(name: str) -> list[tuple[str, str]]:
    rows = []
    p = PEOPLE_DIR / name / "chat_log.jsonl"
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        c = (d.get("content") or "").strip()
        if c and not is_junk(c):
            rows.append((d.get("sender", "them"), c))
    return rows


def exchanges(rows: list[tuple[str, str]]) -> list[dict]:
    """Every (their message -> my reply burst) pair, with its position."""
    out, i = [], 0
    while i < len(rows) - 1:
        if rows[i][0] == "them" and rows[i + 1][0] == "me":
            burst, j = [], i + 1
            while j < len(rows) and rows[j][0] == "me":
                burst.append(rows[j][1])
                j += 1
            them = rows[i][1]
            if (6 < len(them) < 90 and burst and len(burst) <= 4
                    and all(0 < len(b) < 90 for b in burst)
                    and "sent an attachment" not in them.lower()
                    and not them.startswith("http")):
                out.append({"them": them, "me": burst, "at": i})
            i = j
        else:
            i += 1
    return out


def main() -> None:
    if OUT.exists() and "--force" not in sys.argv:
        raise SystemExit(
            f"{OUT.name} already exists. The whole point is that it stays "
            "fixed - regenerating invalidates every past eval number. "
            "Use --force if you really mean it."
        )

    v = json.loads((ROOT / "voice.json").read_text(encoding="utf-8"))
    people = sorted(v["people"],
                    key=lambda k: -v["people"][k]["style"]["messages_counted"])

    rng = random.Random(SEED)
    cases = []
    for name in people[:12]:
        ex = exchanges(load(name))
        if len(ex) < PER_PERSON * 2:
            continue
        rng.shuffle(ex)
        for e in ex[:PER_PERSON]:
            cases.append({"person": name, "them": e["them"], "me": e["me"]})

    rng.shuffle(cases)
    OUT.write_text(json.dumps({
        "_readme": (
            "Frozen held-out test set for eval_voice.py. Do not regenerate "
            "casually - past eval numbers are only comparable against this "
            "exact set. build_voice.py excludes these exchanges from few-shot "
            "examples so the model never sees the answer."
        ),
        "seed": SEED,
        "cases": cases,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"wrote {OUT.name}: {len(cases)} cases across "
          f"{len({c['person'] for c in cases})} people")
    multi = sum(1 for c in cases if len(c["me"]) >= 2)
    emo = sum(1 for c in cases
              if any(ord(ch) > 0x2500 for m in c["me"] for ch in m))
    print(f"  multi-message: {multi}  with emoji: {emo}")


if __name__ == "__main__":
    main()

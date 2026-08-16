"""Score Nemotron's replies against the measured voice.

Held-out evaluation: takes real (their message -> my actual reply) pairs from
the corpus, hides my reply, asks the model, and compares style. Also flags the
specific failure modes seen while building this.

  python eval_voice.py            # 24 cases across the top people
  python eval_voice.py --n 8      # quick pass
  python eval_voice.py --show     # print every case
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from statistics import mean, median

from shadow import brain, memory
from shadow.config import ROOT
from shadow.memory import Msg

SWEAR = brain._SWEAR_RE if hasattr(brain, "_SWEAR_RE") else re.compile(
    r"\b(bhosad|madarchod|laudi|randi|bhadva|bhadve|chutiya|gaand|sale|saale|"
    r"mkc|bkl|bc|wtf|fuck|fucking|shit|bitch|harami|bsdk)\b", re.I)
HINDI = re.compile(
    r"\b(hai|hain|nhi|nahi|mai|kya|kyu|koi|bhai|toh|tha|thi|raha|rha|kar|hua|"
    r"bhi|aur|ye|vo|wo|se|ka|ke|ki|ko|pe|na|ha|haan|aare|abe|aabe|yaar|bas|"
    r"kuch|sab|acha|thik|chal|dekh|sun|bol|kal|aaj|matlab|mereko|mujhe)\b", re.I)


EVAL_SET = ROOT / "eval_cases.json"


def cases(n: int) -> list[tuple[str, str, list[str]]]:
    """(person, their message, my real reply) from the FROZEN held-out set.

    These used to be drawn from voice.json's examples, which build_voice.py
    reshuffles on every rebuild - so every run scored a different test set and
    his own reply stats drifted between runs (for example, the language mix), making real
    gains indistinguishable from sampling noise. Fixed set, same cases forever.
    """
    if not EVAL_SET.exists():
        raise SystemExit(f"{EVAL_SET.name} missing. Run: python make_eval_set.py")
    d = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    return [(c["person"], c["them"], c["me"]) for c in d["cases"]][:n]


def score(person: str, got: list[str], real: list[str], their: str) -> dict:
    entry, _ = brain._register(person, {})
    st = (entry or {}).get("style", {})
    g = " ".join(got)
    ex_pool = {m for e in (entry or {}).get("examples", []) for m in e["me"]}

    return {
        "n_msgs": len(got),
        "chars": int(median([len(x) for x in got])) if got else 0,
        "target_chars": st.get("median_chars", 17),
        "hinglish": bool(HINDI.search(g)),
        "swear": bool(SWEAR.search(g)),
        "emoji": any(ord(c) > 0x2500 for c in g),
        "target_emoji_pct": st.get("pct_with_emoji", 14.0),
        # Paired truth: what the owner ACTUALLY replied to this same message.
        # A global average is the wrong yardstick when the sample is not a
        # random draw - most eval prompts are not funny, so low emoji may be
        # correct here even though his overall rate is higher.
        "real_n_msgs": len(real),
        "real_chars": int(median([len(x) for x in real])) if real else 0,
        "real_emoji": any(ord(c) > 0x2500 for x in real for c in x),
        "real_swear": bool(SWEAR.search(" ".join(real))),
        "real_hinglish": bool(HINDI.search(" ".join(real))),
        # failure modes seen during development
        "echoed": any(x.lower().strip("?. ") == their.lower().strip("?. ")
                      for x in got),
        # Only DISTINCTIVE overlap counts. A third of the example replies are
        # <=10 chars ("wow", "k", "fr", "😂😂") and those are just his normal
        # vocabulary - flagging them as copying was a false positive.
        "copied_example": any(x in ex_pool and len(x) > 12 for x in got),
        "too_long": any(len(x) > 90 for x in got),
        "too_many": len(got) > 4,
        "empty": not got,
        "has_period": any(x.endswith(".") for x in got),
        "meta": any(re.match(r"^(them|you|me|note|reply|based on)\b", x, re.I)
                    for x in got),
    }


def main() -> None:
    n = 24
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])
    show = "--show" in sys.argv

    rows = []
    errors = 0
    frozen_n = 0
    for person, their, real in cases(n):
        prof = memory.profile(person)
        past = memory.history(person, 20)
        # Drop the held-out exchange from BOTH places it appears, or the model
        # is handed the answer and "copying" is just reading.
        past = [m for m in past if m.content not in real and m.content != their]
        # Bedrock drops the odd connection. One lost request should not throw
        # away a 40-case run, so retry twice then record it as an error.
        d = None
        for attempt in range(3):
            try:
                d = brain.reply(person, prof, [Msg("them", their)], past,
                                hide_example=their)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  [error] {person}: {type(e).__name__}")
                else:
                    time.sleep(2 * (attempt + 1))
        if d is None:
            errors += 1
            continue
        if d.action == "freeze":
            # Refusing to answer a money/grief/confession message is correct
            # behaviour, not a miss. Excluded rather than scored.
            frozen_n += 1
            if show:
                print(f"\n{person} <- {their}\n   FROZEN [{d.note}] - correct, skipped")
            continue
        got = d.messages if d.action == "reply" else []
        s = score(person, got, real, their)
        s["person"], s["their"], s["got"], s["real"] = person, their, got, real
        s["skipped"] = d.action != "reply"
        rows.append(s)
        if show:
            print(f"\n{person} <- {their}")
            print(f"   real: {real}")
            print(f"   got:  {got}"
                  + ("  [SKIP]" if s["skipped"] else ""))
            bad = [k for k in ("echoed", "copied_example", "too_long", "too_many",
                               "empty", "meta") if s[k]]
            if bad:
                print(f"   FLAGS: {', '.join(bad)}")

    ok = [r for r in rows if not r["empty"]]
    print(f"\n{'='*58}\nEVAL  {len(rows)} scored"
          + (f"   ({frozen_n} correctly frozen)" if frozen_n else "")
          + (f"   ({errors} network errors)" if errors else ""))
    if ok:
        # Compared against what the owner actually replied to these same
        # messages, not a corpus-wide average over a different distribution.
        def gm(k): return median([r[k] for r in ok])
        def ga(k): return mean([r[k] for r in ok])
        def gp(k): return sum(r[k] for r in ok) / len(ok) * 100

        print("                        model    him (same cases)")
        print(f"  median reply length   {int(gm('chars')):5}    {int(gm('real_chars')):5}")
        print(f"  messages per reply    {ga('n_msgs'):5.2f}    {ga('real_n_msgs'):5.2f}")
        print(f"  hinglish              {gp('hinglish'):4.0f}%    {gp('real_hinglish'):4.0f}%")
        print(f"  emoji                 {gp('emoji'):4.0f}%    {gp('real_emoji'):4.0f}%")
        print(f"  swearing              {gp('swear'):4.0f}%    {gp('real_swear'):4.0f}%")
    for k, label in [("echoed", "echoed their message"),
                     ("copied_example", "copied a few-shot example"),
                     ("too_long", "message over 90 chars"),
                     ("too_many", "more than 4 messages"),
                     ("meta", "leaked meta/narration"),
                     ("has_period", "ended with a full stop"),
                     ("empty", "empty / skipped")]:
        c = sum(r[k] for r in rows)
        mark = "FAIL" if (c and k in ("echoed", "copied_example", "meta",
                                      "too_many", "too_long")) else "    "
        print(f"  {mark} {label:28} {c}/{len(rows)}")


if __name__ == "__main__":
    main()

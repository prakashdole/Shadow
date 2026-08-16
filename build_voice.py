"""Build voice.json — the whole voice spec, mined from the real corpus.

Everything the model needs to sound like the account owner lives in one JSON file:
global style stats, vocabulary, per-person register, and real reply examples.
No prose, no markdown. Rebuild any time history changes:

    python build_voice.py

The point is that every number here is measured, not asserted. The old
hand-written personality.md was wrong on nearly every claim (it said "aabe" was
the top opener — it is 0.9% of messages) because it was written from one
conversation and generalised.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from statistics import mean, median

from shadow.config import PEOPLE_DIR, ROOT
from shadow.memory import is_junk

OUT = ROOT / "voice.json"
EVAL_SET = ROOT / "eval_cases.json"


def held_out() -> set[str]:
    """Their-side text of every frozen eval case.

    These exchanges are excluded from the few-shot examples. Without this the
    model is handed the answer to its own test and "copying" is just reading.
    """
    if not EVAL_SET.exists():
        return set()
    d = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    return {c["them"].strip().lower() for c in d.get("cases", [])}

SWEARS = ["bhosad", "madarchod", "laudi", "randi", "bhadva", "bhadve", "chutiya",
          "gaand", "sale", "saale", "mkc", "bkl", "bc", "wtf", "fuck", "fucking",
          "shit", "bitch", "harami", "kutta", "kamina", "bsdk", "ghewar"]
_SWEAR_RE = re.compile(r"\b(" + "|".join(SWEARS) + r")\b", re.I)

# Devanagari-free Hinglish markers — words that only show up in Hindi-in-Latin.
HINDI_MARKERS = re.compile(
    r"\b(hai|hain|nhi|nahi|mai|mera|meri|tera|teri|kya|kyu|kyun|koi|bhai|toh|"
    r"tha|thi|raha|rha|rhi|karna|kar|karo|hua|hoga|hota|bhi|aur|ye|vo|wo|se|ka|"
    r"ke|ki|ko|pe|par|abhi|kal|aaj|matlab|acha|accha|thik|theek|chal|dekh|sun|"
    r"bol|jao|aao|dena|lena|gaya|gayi|liye|wala|wali|na|ha|haan|arre|aare|abe|"
    r"aabe|yaar|yr|bas|kuch|sab|log|din|baat|apna|majha|jaldi)\b", re.I)


def load() -> dict[str, list[tuple[str, str]]]:
    """{person: [(sender, content), ...]} with junk already dropped."""
    out = {}
    for p in sorted(PEOPLE_DIR.glob("*/chat_log.jsonl")):
        rows = []
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
        if rows:
            out[p.parent.name] = rows
    return out


def emojis(texts: list[str]) -> Counter:
    """Real emoji only. Pasted content dragged in CJK text and fullwidth
    punctuation, which are >0x2500 but are not emoji."""
    c = Counter()
    for t in texts:
        for ch in t:
            o = ord(ch)
            if o < 0x2500 or ch == "️":
                continue
            if 0x3000 <= o <= 0x9FFF:      # CJK punctuation + ideographs
                continue
            if 0xFF00 <= o <= 0xFFEF:      # fullwidth forms
                continue
            c[ch] += 1
    return c


def pct(items: list, f) -> float:
    return round(sum(1 for x in items if f(x)) / max(1, len(items)) * 100, 1)


def runs_of_mine(rows: list[tuple[str, str]]) -> list[int]:
    out, cur = [], 0
    for s, _ in rows:
        if s == "me":
            cur += 1
        else:
            if cur:
                out.append(cur)
            cur = 0
    if cur:
        out.append(cur)
    return out


def style(msgs: list[str]) -> dict:
    """Measured style of one bucket of my messages."""
    if not msgs:
        return {}
    lens = [len(m) for m in msgs]
    return {
        "messages_counted": len(msgs),
        "median_chars": int(median(lens)),
        "mean_words": round(mean(len(m.split()) for m in msgs), 1),
        "pct_under_15_chars": pct(msgs, lambda m: len(m) < 15),
        "pct_single_word": pct(msgs, lambda m: len(m.split()) == 1),
        "pct_with_emoji": pct(msgs, lambda m: any(ord(c) > 0x2500 for c in m)),
        "pct_with_swear": pct(msgs, lambda m: _SWEAR_RE.search(m)),
        "pct_starts_capital": pct(msgs, lambda m: m[:1].isupper()),
        "pct_with_apostrophe": pct(msgs, lambda m: "'" in m),
        "pct_ends_with_period": pct(msgs, lambda m: m.endswith(".")),
        "pct_all_caps": pct(msgs, lambda m: m.isupper() and len(m) > 2),
        "pct_hinglish": pct(msgs, lambda m: HINDI_MARKERS.search(m)),
        "pct_over_120_chars": pct(msgs, lambda m: len(m) > 120),
    }


def pairs(rows: list[tuple[str, str]], limit: int,
          exclude: set[str] | None = None) -> list[dict]:
    """Real (their message -> my reply burst) pairs. These are the few-shots.

    The model imitates these far more than it obeys the percentages in the
    spec, so the SAMPLE has to look like the real distribution. Taking the
    first N chronologically gave 6-of-8 single-message, 0-of-8 with emoji,
    and the model went mute and emoji-free to match. So: collect every
    candidate, then fill quotas for multi-message and emoji replies.

    `exclude` holds the held-out eval cases, which must never appear here.
    """
    exclude = exclude or set()
    cands = []
    i = 0
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
                    and not them.startswith("http")
                    and them.strip().lower() not in exclude):
                cands.append({"them": them, "me": burst})
            i = j
        else:
            i += 1

    if len(cands) <= limit:
        return cands

    def has_emoji(e: dict) -> bool:
        return any(ord(c) > 0x2500 for m in e["me"] for c in m)

    def is_grunt(e: dict) -> bool:
        """Every message a one-word acknowledgement ("ha", "yea", "k"). Real,
        but the model latches onto these and answers everything with them.

        An emoji-only reply is NOT a grunt even though it is short: sending
        🤣🤣🤣 as a whole message is a distinct, high-signal habit, and
        demoting it by length alone stripped it out of the examples and the
        model stopped using emoji entirely.
        """
        if any(has_emoji({"me": [m]}) for m in e["me"]):
            return False
        return all(len(m) <= 6 for m in e["me"])

    def is_hinglish(e: dict) -> bool:
        return bool(HINDI_MARKERS.search(" ".join(e["me"])))

    multi = [e for e in cands if len(e["me"]) >= 2]
    emo = [e for e in cands if has_emoji(e)]
    longer = sorted(cands, key=lambda e: -max(len(m) for m in e["me"]))
    # Fill from substantive exchanges first; grunts only top up the tail.
    meaty = [e for e in cands if not is_grunt(e)]
    # Language balance matters: taking whatever came first gave 63% Hinglish
    # examples against a 39% corpus rate, and the model then forced Hindi into
    # replies to English messages. Keep an explicit English quota.
    english = [e for e in meaty if not is_hinglish(e)]

    picked: list[dict] = []
    seen: set[int] = set()

    def take(pool: list[dict], n: int) -> None:
        for e in pool:
            if len(picked) >= limit or n <= 0:
                return
            k = id(e)
            if k in seen:
                continue
            seen.add(k)
            picked.append(e)
            n -= 1

    # ~40% multi-message, ~25% with emoji, ~15% on the longer end. Prefer the
    # substantive version of each quota, then fill the rest from meaty
    # exchanges, and only fall back to everything (grunts included) last.
    take([e for e in multi if not is_grunt(e)], max(1, round(limit * 0.4)))
    take([e for e in emo if not is_grunt(e)], max(1, round(limit * 0.25)))
    take(english, max(1, round(limit * 0.35)))
    take(longer, max(1, round(limit * 0.15)))
    take(meaty, limit)
    take(cands, limit)
    return picked


def main() -> None:
    data = load()
    # Never let a frozen eval case become a few-shot example.
    heldout = held_out()
    all_mine = [c for rows in data.values() for s, c in rows if s == "me"]
    all_runs = [r for rows in data.values() for r in runs_of_mine(rows)]

    words = Counter()
    for m in all_mine:
        for w in re.findall(r"[a-z']{2,}", m.lower()):
            words[w] += 1

    openers = Counter(m.split()[0].lower() for m in all_mine if m.split())
    shorts = Counter(m.lower() for m in all_mine if len(m) <= 6)
    emo = emojis(all_mine)

    # "edited" is an Instagram UI artifact appended to edited messages, not a
    # word I type. Everything else here is an English function word.
    STOP = {"the", "and", "to", "a", "in", "it", "is", "of", "this", "that", "for",
            "with", "but", "so", "on", "are", "no", "not", "have", "was", "do",
            "how", "can", "see", "my", "will", "me", "then", "you", "i",
            "edited", "at", "be", "if", "or", "as", "an", "am", "its", "im"}
    slang = [w for w, n in words.most_common(200)
             if w not in STOP and n >= 12][:45]

    # per-person register, only where there is enough of me to measure
    people = {}
    for name, rows in data.items():
        mine = [c for s, c in rows if s == "me"]
        if len(mine) < 15:
            continue
        prof_path = PEOPLE_DIR / name / "profile.json"
        prof = {}
        if prof_path.exists():
            try:
                prof = json.loads(prof_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        ctx_path = PEOPLE_DIR / name / "context.md"
        st = style(mine)
        people[name] = {
            "display_name": prof.get("display_name", name),
            "relationship": prof.get("relationship", ""),
            "closeness": prof.get("closeness", 5),
            "instagram": (prof.get("platform_usernames") or {}).get("instagram", ""),
            "notes": (ctx_path.read_text(encoding="utf-8").strip()[:400]
                      if ctx_path.exists() else ""),
            "style": st,
            "my_top_words": [w for w, _ in Counter(
                w for m in mine for w in re.findall(r"[a-z']{2,}", m.lower())
                if w not in STOP).most_common(12)],
            "my_emoji": [e for e, _ in emojis(mine).most_common(5)],
            "examples": pairs(rows, 8, exclude=heldout),
        }

    voice = {
        "_readme": (
            "Measured voice spec for the account owner, generated by build_voice.py "
            "from the real chat corpus. Every number is observed, not asserted. "
            "Regenerate after importing more history."
        ),
        "identity": {},
        "global_style": style(all_mine),
        "burst_behaviour": {
            "_note": "How many messages I send in a row before they reply.",
            "runs_measured": len(all_runs),
            "mean_messages_per_turn": round(mean(all_runs), 2),
            "pct_single_message": round(
                sum(1 for r in all_runs if r == 1) / len(all_runs) * 100, 1),
            "pct_two_or_three": round(
                sum(1 for r in all_runs if 2 <= r <= 3) / len(all_runs) * 100, 1),
            "pct_four_plus": round(
                sum(1 for r in all_runs if r >= 4) / len(all_runs) * 100, 1),
        },
        "vocabulary": {
            "top_openers": [{"word": w, "count": n} for w, n in openers.most_common(20)
                            if w.isalpha()][:15],
            "common_short_replies": [{"text": t, "count": n}
                                     for t, n in shorts.most_common(30)
                                     if any(ch.isalpha() for ch in t)][:22],
            "slang_and_hinglish": slang,
            "emoji_ranked": [{"emoji": e, "count": n} for e, n in emo.most_common(10)],
        },
        "people": people,
        "examples_global": [],
    }

    # a spread of examples across people, for the system prompt
    for name in sorted(people, key=lambda n: -people[n]["style"]["messages_counted"])[:8]:
        voice["examples_global"].extend(people[name]["examples"][:3])

    OUT.write_text(json.dumps(voice, ensure_ascii=False, indent=2), encoding="utf-8")

    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT.name}  ({kb:.1f} KB)")
    print(f"  my messages analysed: {len(all_mine)}")
    print(f"  people with a register: {len(people)}")
    print(f"  global examples: {len(voice['examples_global'])}")
    print(f"  median msg: {voice['global_style']['median_chars']} chars, "
          f"{voice['global_style']['pct_single_word']}% single-word, "
          f"{voice['global_style']['pct_with_swear']}% swear")


if __name__ == "__main__":
    main()

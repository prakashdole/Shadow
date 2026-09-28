"""Alignment — mapping a read window onto what is already stored (D31).

P00b found that the rendered DOM exposes no per-message id, so the same message
seen twice cannot be recognised by identity. What is left is ordered sequence
alignment over overlapping windows, and this module is the whole of it: a pure
function from (stored tail, window) to "where does the window start being new".

The rules, and why each one exists:

* Nothing is ever de-duplicated on text. "ha" three times is three events, and
  two identical replies in a row are two events (D32 defect 3). De-duplication
  happens by finding where the window's prefix matches the stored suffix; the
  same text at a NEW position is new.
* An empty store is a `baseline`: the whole window is history. That is what the
  first read of a thread is (D40), and it must not become reply work.
* If no prefix of the window matches the stored tail, the window does not reach
  known history. That is `gap`, and the caller holds rather than guessing.
* Several matching lengths are possible when a conversation repeats itself. Two
  or more identical messages in a row (`ha`, `ha`, `ha`) match at every length,
  and guessing wrong there silently invents or drops events. So multiple
  candidates are `ambiguous` unless the longest overlap is long enough AND
  carries at least two distinct keys, which is the case where the extra evidence
  makes the longest answer the only sane one.
* Ordering is never repaired. If the window disagrees with the stored order, no
  k matches and the result is `gap`. Sorting a window to force a match would
  hide exactly the reordering this module exists to detect.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

AlignStatus = Literal["ok", "baseline", "gap", "ambiguous"]

# (sender, kind, normalized text)
Key = tuple[str, str, str]

# \s on a str pattern is Unicode-aware, so this covers tabs, newlines and the
# exotic spaces (non-breaking, thin, ideographic) that arrive in emoji alt text
# and in pasted text.
_WHITESPACE_RUN = re.compile(r"\s+")

# The overlap must be at least this long, and must contain at least this many
# distinct keys, before the longest candidate is trusted over a shorter one.
MIN_TRUSTED_OVERLAP = 3
MIN_DISTINCT_KEYS = 2


def normalize_text(text: str | None) -> str:
    """NFC, whitespace collapsed, trimmed.

    No case folding and no punctuation stripping: "OK" and "ok" are different
    messages as far as this module is concerned, and a fold would merge two
    genuinely different texts. Unicode normalization is done because the same
    composed and decomposed string is one message, and emoji alt text arrives in
    either form.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFC", text)
    return _WHITESPACE_RUN.sub(" ", folded).strip()


def key_of(sender: str, kind: str, text: str | None) -> Key:
    """The alignment key for one event. `kind` is part of it so a media bubble
    and a text bubble with the same label never align to each other."""
    return (str(sender), str(kind), normalize_text(text))


def keys_of(events: Iterable) -> list[Key]:
    """Keys for stored event rows or ObservedEvent dataclasses.

    Accepts either because the caller has both: the tail comes from SQLite rows
    (dicts) and the window from `ObservedEvent` (contracts). A row missing a
    field is an `unknown` sender at `unknown` kind rather than a guess.
    """
    out: list[Key] = []
    for event in events:
        if isinstance(event, dict):
            out.append(
                key_of(
                    event.get("sender") or "unknown",
                    event.get("kind") or "unknown",
                    event.get("text"),
                )
            )
        else:
            out.append(
                key_of(
                    getattr(event, "sender", None) or "unknown",
                    getattr(event, "kind", None) or "unknown",
                    getattr(event, "text", None),
                )
            )
    return out


@dataclass(frozen=True)
class AlignResult:
    """Where the window stops being known history.

    `new_start_index` is an index into the window: `window[new_start_index:]` are
    the events this read adds. For `baseline` it is 0 (all of it is history); for
    `gap` and `ambiguous` it is -1 and the caller must not store anything from
    this window.
    """

    status: AlignStatus
    new_start_index: int
    overlap: int = 0
    candidates: tuple[int, ...] = ()
    reason: str = ""

    @property
    def is_new(self) -> bool:
        return self.status == "ok" and self.new_start_index >= 0

    def new_events(self, window: Sequence[Key]) -> list[Key]:
        """The keys this read adds. Empty unless the result is `ok`."""
        if not self.is_new:
            return []
        return list(window[self.new_start_index:])


def align(stored_tail_keys: Sequence[Key], window_keys: Sequence[Key]) -> AlignResult:
    """Align one read window against the tail of what is stored.

    Never raises for ordinary input: a caller in a live cycle gets a status it
    can hold on, not an exception it has to guess how to handle.
    """
    stored = list(stored_tail_keys)
    window = list(window_keys)
    if not stored:
        return AlignResult(
            status="baseline",
            new_start_index=0,
            reason="nothing stored yet: the whole window is history",
        )
    if not window:
        return AlignResult(
            status="ok",
            new_start_index=0,
            overlap=0,
            candidates=(0,),
            reason="empty window adds nothing",
        )

    # Every k >= 1 whose window prefix equals the stored tail's last k keys.
    # Only the stored tail can overlap, so k is bounded by both lengths.
    candidates = tuple(
        k
        for k in range(1, min(len(window), len(stored)) + 1)
        if window[:k] == stored[-k:]
    )
    if not candidates:
        return AlignResult(
            status="gap",
            new_start_index=-1,
            reason="no prefix of the window matches the stored tail: "
            "the window does not reach known history",
        )
    longest = max(candidates)
    if len(candidates) == 1:
        return AlignResult(
            status="ok",
            new_start_index=longest,
            overlap=longest,
            candidates=candidates,
            reason="one matching overlap",
        )
    distinct = len(set(window[:longest]))
    if longest >= MIN_TRUSTED_OVERLAP and distinct >= MIN_DISTINCT_KEYS:
        return AlignResult(
            status="ok",
            new_start_index=longest,
            overlap=longest,
            candidates=candidates,
            reason=f"longest overlap {longest} carries {distinct} distinct keys; "
            "the shorter candidates are repeats inside it",
        )
    return AlignResult(
        status="ambiguous",
        new_start_index=-1,
        overlap=0,
        candidates=candidates,
        reason="overlap lengths "
        f"{list(candidates)} all match and the longest carries only {distinct} "
        "distinct key(s): holding rather than guessing",
    )


def trailing_unanswered_count(stored_keys: Sequence[Key], from_index: int) -> int:
    """How many contact events follow the last owner event in `stored_keys`.

    Used on a baseline: history that ends with the contact talking is context
    the owner may want to see once, and it is never reply work (D40).
    """
    last_owner = -1
    for index, key in enumerate(stored_keys):
        if key[0] == "owner":
            last_owner = index
    if last_owner == len(stored_keys) - 1:
        return 0
    return sum(1 for key in stored_keys[last_owner + 1:] if key[0] == "contact")

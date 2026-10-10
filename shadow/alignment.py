"""Alignment — mapping a read window onto what is already stored (D31).

P00b found no stable per-message DOM attribute. A separately qualified runtime
signal can corroborate placement, but ordinary reads still use ordered sequence
alignment over overlapping windows. This module is the whole of it: a pure
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
* Every competing placement is ambiguous, including long alternating repeats.
  Distinct text does not prove identity. A uniquely placed full stored block in
  a deeper window can anchor its new tail; older context is never new work.
* Ordering is never repaired. If the window disagrees with the stored order, no
  k matches and the result is `gap`. Sorting a window to force a match would
  hide exactly the reordering this module exists to detect.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

AlignStatus = Literal["ok", "baseline", "gap", "ambiguous"]

# Sender, kind, normalized text, media label, quoted text; no mutable badges.
Key = tuple[str, str, str, str, str]
RUNTIME_ID_SOURCE = "react_current_message_v1"
RUNTIME_ID_MAX_LENGTH = 512

# \s on a str pattern is Unicode-aware, so this covers tabs, newlines and the
# exotic spaces (non-breaking, thin, ideographic) that arrive in emoji alt text
# and in pasted text.
_WHITESPACE_RUN = re.compile(r"\s+")

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


def key_of(sender: str, kind: str, text: str | None,
           media_label: str | None = None, reply_to: str | None = None) -> Key:
    """The alignment key for one event. `kind` is part of it so a media bubble
    and a text bubble with the same label never align to each other."""
    return (str(sender), str(kind), normalize_text(text),
            normalize_text(media_label), normalize_text(reply_to))


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
                    event.get("media_label"),
                    event.get("reply_to"),
                )
            )
        else:
            out.append(
                key_of(
                    getattr(event, "sender", None) or "unknown",
                    getattr(event, "kind", None) or "unknown",
                    getattr(event, "text", None),
                    getattr(event, "media_label", None),
                    getattr(event, "reply_to_text", None),
                )
            )
    return out


def sequence_events(events: Iterable) -> list:
    """Reaction badges are mutable observation metadata, not sequence anchors.

    Keep legacy reaction rows intact in storage, but omit them on both sides of
    matching so badge removal cannot manufacture a gap or new message.
    """
    return [e for e in events if (e.get('kind') if isinstance(e, dict)
                                  else getattr(e, 'kind', None)) != 'reaction']


def _evidence_of(event) -> dict:
    if isinstance(event, dict):
        try:
            evidence = json.loads(event.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
    else:
        evidence = getattr(event, "evidence", {})
    return evidence if isinstance(evidence, dict) else {}


def runtime_identity_conflict(event) -> bool:
    """Whether the collector found contradictory runtime identity evidence."""
    evidence = _evidence_of(event)
    return any(evidence.get(key) is True for key in (
        "runtime_identity_conflict", "runtime_outgoing_conflict",
        "runtime_outgoing_sender_conflict",
    ))


def runtime_message_id(event) -> str | None:
    """Return only the independently qualified, page-local runtime identity."""
    evidence = _evidence_of(event)
    value = evidence.get("runtime_message_id") if isinstance(evidence, dict) else None
    return (value if isinstance(value, str) and value and len(value) <= RUNTIME_ID_MAX_LENGTH
            and evidence.get("runtime_identity_source") == RUNTIME_ID_SOURCE else None)


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


def align(stored_tail_keys: Sequence[Key], window_keys: Sequence[Key], *,
          allow_containment: bool = True) -> AlignResult:
    """Align one read window against the tail of what is stored.

    History collectors merging an older window with a newer window disable
    containment: only a suffix-to-prefix overlap proves that merge direction.
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
    endpoints = {
        k
        for k in range(1, min(len(window), len(stored)) + 1)
        if window[:k] == stored[-k:]
    }
    if allow_containment:
        endpoints.update(start + len(stored)
                         for start in range(1, len(window) - len(stored) + 1)
                         if window[start:start + len(stored)] == stored)
    candidates = tuple(sorted(endpoints))
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
            overlap=min(longest, len(stored)),
            candidates=candidates,
            reason="one contiguous placement",
        )
    return AlignResult(
        status="ambiguous",
        new_start_index=-1,
        overlap=0,
        candidates=candidates,
        reason=f"competing new-tail offsets {list(candidates)}: holding rather than guessing",
    )


def align_events(stored, window, *, allow_containment: bool = True) -> AlignResult:
    """Align with qualified runtime identities when they are present and consistent.

    Runtime identity proves placement only.  A same-ID content change is held,
    not treated as a DOM edit, because this optional source cannot establish an
    edit lifecycle.
    """
    stored, window = list(stored), list(window)
    if any(runtime_identity_conflict(event) for event in (*stored, *window)):
        return AlignResult("gap", -1, reason="runtime identity conflict evidence")
    stored_ids = [runtime_message_id(event) for event in stored]
    window_ids = [runtime_message_id(event) for event in window]
    if any(ids.count(value) > 1 for ids in (stored_ids, window_ids) for value in set(ids) - {None}):
        return AlignResult("ambiguous", -1, reason="runtime identity duplicate")
    stored_keys, window_keys = keys_of(stored), keys_of(window)
    shared = set(stored_ids) & set(window_ids) - {None}
    for identity in shared:
        if stored_keys[stored_ids.index(identity)] != window_keys[window_ids.index(identity)]:
            return AlignResult("gap", -1, reason="runtime identity content conflict")
    aligned = align(stored_keys, window_keys, allow_containment=allow_containment)
    if not any(stored_ids) and not any(window_ids):
        return aligned
    candidates = []
    for endpoint in aligned.candidates:
        overlap = min(endpoint, len(stored_ids))
        start = endpoint - overlap
        pairs = tuple(zip(stored_ids[-overlap:], window_ids[start:endpoint]))
        matched = {left for left, right in pairs if left is not None and left == right}
        if shared <= matched and all(left is None or right is None or left == right
                                     for left, right in pairs):
            candidates.append(endpoint)
    if not candidates and aligned.candidates:
        return AlignResult("gap", -1, reason="runtime identity ordered placement conflict")
    if len(candidates) == 1:
        endpoint = candidates[0]
        return AlignResult("ok", endpoint, overlap=min(endpoint, len(stored_ids)),
                           candidates=(endpoint,), reason="one contiguous placement")
    if len(candidates) > 1:
        return AlignResult("ambiguous", -1, candidates=tuple(candidates),
                           reason=f"competing new-tail offsets {candidates}: holding rather than guessing")
    return aligned


def is_runtime_identity_hold(result: AlignResult) -> bool:
    """Whether an identity conflict must not fall back to content reconciliation."""
    return result.reason.startswith("runtime identity ")


def runtime_backfill_pairs(stored, window, *, allow_containment: bool = True) -> list[tuple[int, str]]:
    """Map legacy rows to qualified IDs only after independently unique content alignment."""
    matched = align(keys_of(stored), keys_of(window), allow_containment=allow_containment)
    if matched.status != "ok" or len(matched.candidates) != 1 or not matched.overlap:
        return []
    start = matched.new_start_index - matched.overlap
    targets = stored[len(stored) - matched.overlap:]
    observed = window[start:matched.new_start_index]
    return [(target["id"], identity) for target, event in zip(targets, observed)
            if target.get("id") is not None and runtime_message_id(target) is None
            and (identity := runtime_message_id(event)) is not None]


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

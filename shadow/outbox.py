"""Outbox worker — one item at a time, honestly (D04, D05, D06, D08).

The old code sent a burst and returned a boolean. That is how a partial burst
became a reported success: two of three messages could fail and the caller could
not tell. This module replaces the boolean with a per-item result and makes three
things impossible:

1. **A blind resend.** `attempting` is persisted BEFORE the adapter is called, so
   a process that dies anywhere after that leaves a row that startup turns into
   `uncertain` and then `held`. Nothing is ever retried from `uncertain`.
2. **A confirmed send without evidence.** Confirmation goes through
   `Storage.confirm_send`, which writes the sent event and the `confirmed` state
   in one transaction, and only from `attempting` or `uncertain` for that item's
   own conversation. A `SendResult` naming a different thread is not
   confirmation; it is uncertainty.
3. **Silently regenerated bursts.** An uncertain item stops the burst. The
   remaining items are not attempted, and are then cancelled with the reason
   rather than left looking sendable.

A pre-submit failure is the one recoverable case, and it is bounded: at most
`MAX_PRE_SUBMIT_RETRIES` further attempts, each only if the caller's check passes
again, with a backoff through the injected clock. Nothing here sleeps on the wall
clock and nothing here decides policy: whether a send is allowed at all belongs
to the coordinator's `check` callback, which is awaited before typing, before
Enter and between items.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from shadow.contracts import BrowserPort, CheckPoint, SendCheck, SendResult
from shadow.pacing import Clock
from shadow import storage as ST
from shadow.storage import Storage, iso

# The TRD allows a safe pre-submit failure "at most twice"; these are the two
# numbers that sentence implies and that have no row in the settings table.
MAX_PRE_SUBMIT_RETRIES = 2  # PROPOSED SETTING: pre_submit_max_retries
RETRY_BACKOFF_SECONDS = 2.0  # PROPOSED SETTING: pre_submit_retry_backoff_seconds

UNCERTAIN_ALERT = "uncertain_send"
DROPPED_ALERT = "burst_dropped"


@dataclass(frozen=True)
class ItemOutcome:
    """What happened to one intended message."""

    item_id: int
    text: str
    state: str
    reason: str = ""
    event_id: int | None = None
    attempts: int = 0

    @property
    def confirmed(self) -> bool:
        return self.state == "confirmed"


@dataclass(frozen=True)
class SendReport:
    outcomes: tuple[ItemOutcome, ...]
    stopped_reason: str = ""

    @property
    def confirmed(self) -> tuple[ItemOutcome, ...]:
        return tuple(o for o in self.outcomes if o.state == "confirmed")

    @property
    def uncertain(self) -> tuple[ItemOutcome, ...]:
        return tuple(o for o in self.outcomes if o.state == "uncertain")

    @property
    def not_attempted(self) -> tuple[ItemOutcome, ...]:
        return tuple(o for o in self.outcomes if o.state == "not_attempted")

    @property
    def failed(self) -> tuple[ItemOutcome, ...]:
        return tuple(o for o in self.outcomes if o.state == "failed_before_submit")

    def state_of(self, item_id: int) -> str:
        for outcome in self.outcomes:
            if outcome.item_id == item_id:
                return outcome.state
        return "absent"


def _thread_id(storage: Storage, conversation) -> str:
    if isinstance(conversation, dict):
        thread_id = conversation.get("thread_id")
    else:
        row = storage.get_conversation(conversation)
        thread_id = row["thread_id"] if row else None
    if not thread_id:
        raise ValueError(
            "a send needs the conversation's exact thread id; refusing to send "
            "without one (rules 13, 14)"
        )
    return str(thread_id)


def _raise_uncertain(
    storage: Storage, conversation_id: int, item: dict, reason: str
) -> None:
    """Mark an item uncertain and raise exactly one urgent alert for it.

    The dedupe key is the item id, so repeated ticks and the restart recovery
    path produce one alert, not one per attempt.
    """
    storage.transition(item["id"], "uncertain", evidence=reason)
    storage.raise_alert(
        dedupe_key=f"{UNCERTAIN_ALERT}:{item['id']}",
        tier="urgent",
        reason=UNCERTAIN_ALERT,
        conversation_id=conversation_id,
    )


def _confirm(
    storage: Storage, conversation_id: int, item: dict, result: SendResult
) -> tuple[ItemOutcome | None, bool]:
    """Try to confirm. Returns (outcome, ok). ok=False means stop the burst."""
    try:
        event_id = storage.confirm_send(item["id"], {"text": item["text"]})
    except Exception as exc:  # noqa: BLE001 - any failure here is uncertainty
        _raise_uncertain(storage, conversation_id, item, f"confirm_failed:{type(exc).__name__}")
        return (
            ItemOutcome(
                item["id"], item["text"], "uncertain", f"confirm_failed:{type(exc).__name__}"
            ),
            False,
        )
    storage.set_outbox_evidence(
        item["id"],
        json.dumps(
            {
                "matched_position": result.matched_position,
                "error_code": result.error_code,
            }
        ),
    )
    stored = storage.get_outbox_item(item["id"]) or {}
    return (
        ItemOutcome(
            item["id"], item["text"], "confirmed", event_id=event_id,
            attempts=stored.get("attempts", 1),
        ),
        True,
    )


async def _send_one(
    storage: Storage,
    browser: BrowserPort,
    conversation_id: int,
    thread_id: str,
    item: dict,
    check: SendCheck,
    clock: Clock,
) -> ItemOutcome:
    item_id = item["id"]
    while True:
        # Persisted before any input. A crash from here on is uncertain.
        storage.transition(item_id, "attempting")
        try:
            result = await browser.send_one(thread_id, item["text"], check)
        except Exception as exc:  # noqa: BLE001 - the adapter died mid-call
            reason = f"send_raised:{type(exc).__name__}"
            _raise_uncertain(storage, conversation_id, item, reason)
            return ItemOutcome(item_id, item["text"], "uncertain", reason)

        if result.status == "confirmed":
            # Confirmation is only valid for this item's own thread. An
            # observation from elsewhere is not evidence for this send.
            if result.thread_id and str(result.thread_id) != thread_id:
                reason = f"wrong_thread:{result.thread_id}"
                _raise_uncertain(storage, conversation_id, item, reason)
                return ItemOutcome(item_id, item["text"], "uncertain", reason)
            outcome, keep_going = _confirm(storage, conversation_id, item, result)
            return outcome

        if result.status == "failed_before_submit":
            reason = result.error_code or "failed_before_submit"
            storage.transition(item_id, "failed_before_submit", evidence=reason)
            attempts = (storage.get_outbox_item(item_id) or {}).get("attempts", 1)
            if attempts <= MAX_PRE_SUBMIT_RETRIES and await check("between_items"):
                storage.transition(item_id, "pending")
                await clock.sleep(RETRY_BACKOFF_SECONDS)
                continue
            return ItemOutcome(item_id, item["text"], "failed_before_submit", reason, attempts=attempts)

        # uncertain, or a status this code does not know: treat as uncertain
        # rather than guessing. An unknown status is never a confirmation.
        reason = result.error_code or f"send_{result.status}"
        _raise_uncertain(storage, conversation_id, item, reason)
        return ItemOutcome(item_id, item["text"], "uncertain", reason)


async def send_items(
    storage: Storage,
    browser: BrowserPort,
    conversation,
    items: Sequence[dict],
    check: SendCheck,
    clock: Clock,
) -> SendReport:
    """Process one burst, in index order, stopping at the first thing that is
    not a clean confirmation.

    `items` are outbox rows; only `pending` ones are attempted. Every item comes
    back in the report, including the ones never attempted, so the caller can
    account for the whole burst.
    """
    conversation_id = conversation["id"] if isinstance(conversation, dict) else int(conversation)
    thread_id = _thread_id(storage, conversation)
    outcomes: list[ItemOutcome] = []
    stopped = ""

    for item in sorted(items, key=lambda row_: row_["id"]):
        if item.get("state") != "pending":
            outcomes.append(
                ItemOutcome(item["id"], item.get("text", ""), item.get("state", "unknown"),
                            "not_attempted")
            )
            continue
        if stopped:
            outcomes.append(
                ItemOutcome(item["id"], item["text"], "not_attempted", stopped)
            )
            continue
        if not await check("between_items"):
            # Pause, takeover, fresh inbound or a mode change landed between
            # bubbles. Nothing further is typed.
            stopped = "check_failed_at_between_items"
            outcomes.append(
                ItemOutcome(item["id"], item["text"], "not_attempted", stopped)
            )
            continue
        outcome = await _send_one(
            storage, browser, conversation_id, thread_id, item, check, clock
        )
        outcomes.append(outcome)
        if outcome.state in ("uncertain", "failed_before_submit"):
            stopped = outcome.reason or outcome.state

    if stopped and any(o.state == "not_attempted" for o in outcomes):
        # The tail of a stopped burst must not keep looking sendable.
        storage.cancel_outbox_items(
            conversation_id, f"burst_stopped:{stopped}", states=("pending",)
        )
        storage.raise_alert(
            dedupe_key=f"{DROPPED_ALERT}:{conversation_id}:{stopped}",
            tier="action",
            reason=DROPPED_ALERT,
            conversation_id=conversation_id,
        )
    return SendReport(tuple(outcomes), stopped)


def recover(storage: Storage) -> int:
    """Startup reconciliation. Never resends.

    `recover_after_restart` turns every interrupted attempt into `uncertain`;
    each of those becomes `held` with one urgent alert, because an unresolved
    attempt is an owner decision, not something to retry on startup.
    """
    moved = storage.recover_after_restart()
    for item in storage.outbox_items():
        if item["state"] != "uncertain":
            continue
        storage.transition(item["id"], "held", evidence="startup_uncertain")
        storage.raise_alert(
            dedupe_key=f"{UNCERTAIN_ALERT}:{item['id']}",
            tier="urgent",
            reason=UNCERTAIN_ALERT,
            conversation_id=item["conversation_id"],
        )
    return moved


def enqueue_owner_send(
    storage: Storage,
    conversation_id: int,
    text: str,
    *,
    now: datetime | None = None,
    revision: int = 0,
) -> tuple[int, int]:
    """Queue an owner-commanded message through the same durable path.

    Bypasses exactly one thing: the reply delay. Pause, identity, takeover and
    the send caps are all still checked when the item is actually sent, because
    this returns an ordinary pending outbox item like any other (D08).
    """
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("an owner command must carry text")
    if storage.get_conversation(conversation_id) is None:
        # Identity is checked before the text is: a command naming a thread that
        # is not a known conversation must not become a queued send.
        raise ST.StorageError(
            f"conversation {conversation_id} does not exist; an owner command "
            f"cannot queue a send for an unverified thread"
        )
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    decision_id = storage.add_decision(
        conversation_id=conversation_id,
        source_revision=revision,
        record_json=json.dumps({"source": "owner_command", "messages": [stripped]}),
        versions_json="{}",
        status="queued",
    )
    item_id = storage.add_outbox_item(
        decision_id=decision_id,
        item_index=0,
        conversation_id=conversation_id,
        text=stripped,
        authority="owner_command",
        revision=revision,
    )
    work = storage.get_work(conversation_id)
    if work is None:
        storage.set_work(
            conversation_id,
            inbound_revision=work_revision(storage, conversation_id),
            due_at=iso(moment),
            first_pending_at=iso(moment),
            state="due",
        )
    else:
        storage.update_work(
            conversation_id,
            state="due",
            due_at=iso(moment),
            first_pending_at=work["first_pending_at"] or iso(moment),
        )
    return decision_id, item_id


def work_revision(storage: Storage, conversation_id: int) -> int:
    work = storage.get_work(conversation_id)
    return 0 if work is None else int(work["inbound_revision"])

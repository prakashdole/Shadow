#!/usr/bin/env python3
"""One explicitly authorised, controlled Instagram test send helper.

This is deliberately not an activation command or a general owner-send CLI.
It accepts one exact current thread, one UUID-labelled test message, and a new
private disposable state directory.  The actual submission always travels
through the production Coordinator and outbox; this module never calls a
browser adapter's ``send_one`` directly.  Import ``controlled_send`` into the
sole already-running live worker: it never attaches to or closes a browser.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from shadow.browser import host_allowed
from shadow.contracts import GLOBAL_PAUSE_STATUSES, ReadResult
from shadow.coordinator import Coordinator, TickReport
from shadow.dom import same_thread, thread_id_from_url
from shadow.operations import private_destination
from shadow.settings import Settings, validate_for_mode
from shadow.storage import open_storage

_THREAD = re.compile(r"^[1-9][0-9]{0,29}$")
_TEST_LABEL = re.compile(r"^\[SHADOW CONTROLLED TEST ([0-9a-f-]{36})\](?:\s|$)", re.I)


@dataclass(frozen=True)
class LiveSendResult:
    """Sanitized proof for a controlled attempt; it intentionally has no text."""

    status: str
    reason: str = ""
    thread_id: str = ""
    outbox_state: str = ""
    attempts: int = 0
    confirmed_event_id: int | None = None
    matched_position: int | None = None
    stopped_reason: str = ""


class _CurrentThreadBrowser:
    """Forbid a coordinator reread from turning this one-message test into navigation."""

    def __init__(self, inner, thread_id: str) -> None:
        self._inner = inner
        self._thread_id = thread_id
        self.navigation_detected = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def read_thread(self, thread_id: str, want: int = 0, *, anchor: tuple = ()) -> ReadResult:
        page = getattr(self._inner, "page", None)
        current_url = getattr(page, "url", "")
        if (thread_id != self._thread_id or not host_allowed(current_url)
                or not same_thread(thread_id_from_url(current_url), self._thread_id)):
            self.navigation_detected = True
            return ReadResult(status="wrong_thread", detail="controlled_test_route_changed")
        result = await self._inner.read_thread(thread_id, want, anchor=anchor)
        current_url = getattr(page, "url", "")
        if (not host_allowed(current_url)
                or not same_thread(thread_id_from_url(current_url), self._thread_id)
                or result.navigations_used):
            self.navigation_detected = True
            return replace(result, status="wrong_thread", thread_id=None,
                           detail="controlled_test_navigation_detected")
        return result


def _validate_inputs(*, account_id: str, expected_account: str, thread_id: str,
                     text: str, settings: Settings, timeout_seconds: float) -> None:
    if not account_id.strip() or not expected_account.strip():
        raise ValueError("exact account id and expected account are required")
    if account_id != expected_account:
        raise ValueError("account id must equal the browser's expected account binding")
    if not _THREAD.fullmatch(thread_id):
        raise ValueError("thread id must be one exact positive numeric runtime id")
    label = _TEST_LABEL.match(text)
    if label is None:
        raise ValueError("message must begin '[SHADOW CONTROLLED TEST <uuid4>]' ")
    try:
        if uuid.UUID(label.group(1)).version != 4:
            raise ValueError
    except ValueError:
        raise ValueError("controlled-test label requires a UUIDv4 nonce") from None
    if not text.strip() or len(text) > settings.bubble_max_chars or len(text) > settings.reply_max_chars:
        raise ValueError("test message exceeds the configured transport ceiling")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValueError("test message contains an unsupported control character")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("a positive explicit overall timeout is required")


def _refused(reason: str, thread_id: str) -> LiveSendResult:
    return LiveSendResult(status="refused", reason=reason, thread_id=thread_id)


async def controlled_send(browser, *, state_dir: Path, account_id: str,
                          expected_account: str, thread_id: str, text: str,
                          settings: Settings, timeout_seconds: float) -> LiveSendResult:
    """Run one owner-directed test via the production coordinator/outbox chain.

    ``state_dir`` must not exist.  It becomes the private disposable SQLite
    evidence directory, so a failed/restarted test cannot replay against an
    existing production state database.
    """
    settings = validate_for_mode(settings)
    if settings.mode != "assisted":
        raise ValueError("controlled test requires an explicitly configured assisted mode")
    _validate_inputs(account_id=account_id, expected_account=expected_account,
                     thread_id=thread_id, text=text, settings=settings,
                     timeout_seconds=timeout_seconds)
    target = private_destination(state_dir, must_be_new=True)
    # The CLI constructs InstagramBrowser with this binding.  Keep the
    # reusable entrypoint equally fail-closed if a caller supplied an adapter
    # whose configured account check is absent or names another account.
    bound_account = getattr(browser, "_expected_account", None)
    if bound_account != expected_account:
        return _refused("expected_account_not_bound", thread_id)

    page = getattr(browser, "page", None)
    current_url = getattr(page, "url", "")
    if not host_allowed(current_url) or not same_thread(thread_id_from_url(current_url), thread_id):
        return _refused("current_thread_not_open", thread_id)

    # Reserve the private evidence directory and take SQLite's writer lock
    # before the first browser await.  A second process cannot replace this
    # fresh state with an existing conversation while the read is in flight.
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.name != "nt":
        os.chmod(target, 0o700)
    with open_storage(target) as storage:
        guarded_browser = _CurrentThreadBrowser(browser, thread_id)
        return await asyncio.wait_for(
            _controlled_send_locked(storage, guarded_browser, account_id, thread_id, text, settings),
            timeout=timeout_seconds,
        )


async def _controlled_send_locked(storage, browser: _CurrentThreadBrowser, account_id: str,
                                  thread_id: str, text: str, settings: Settings) -> LiveSendResult:
    # No inbox listing or label-based recipient discovery: observe only the
    # caller-supplied runtime thread after the disposable state is locked.
    observed: ReadResult = await browser.read_thread(thread_id)
    if browser.navigation_detected:
        return _refused("navigation_detected", thread_id)
    if observed.status in GLOBAL_PAUSE_STATUSES:
        return _refused(observed.status, thread_id)
    if observed.status != "ok":
        return _refused(f"read_{observed.status}", thread_id)
    if str(observed.thread_id) != thread_id:
        return _refused("wrong_thread", thread_id)
    if observed.composer_text:
        return _refused("manual_text", thread_id)
    if observed.is_group is not False:
        return _refused("group_uncertain" if observed.is_group is None else "group", thread_id)

    coordinator = Coordinator(storage, browser, None, settings, account_id=account_id)
    baseline = TickReport()
    # Reuse the coordinator's normal first-observation path so the due
    # re-read aligns to a stored baseline instead of treating history as new.
    await coordinator._handle_read(observed, baseline, label="controlled-test")
    conversation = storage.get_conversation_by_thread(account_id, thread_id)
    if conversation is None or not conversation["first_seen_baseline_done"]:
        return _refused("baseline_not_recorded", thread_id)
    if conversation["paused_reason"]:
        return _refused(conversation["paused_reason"], thread_id)

    # This is the explicit owner-authorized test bootstrap in an otherwise
    # fresh disposable DB.  It permits the owner command; it does not widen
    # autonomy, create a model decision, or affect production contacts.
    storage.set_contact_status(conversation["id"], "approved_unknown",
                               evidence="controlled_test_owner_authorized")
    coordinator.enqueue_owner_send(conversation["id"], text)
    report = await coordinator.tick_due()
    if browser.navigation_detected:
        return _refused("navigation_detected", thread_id)
    items = storage.outbox_items(conversation["id"])
    if len(items) != 1:
        return _refused("outbox_cardinality", thread_id)
    item = items[0]
    event_id = None
    if item["state"] == "confirmed":
        event = next((event for event in storage.get_events(conversation["id"])
                      if event["source"] == "confirmed_send"), None)
        event_id = event["id"] if event else None
    # Failure evidence is a short reason code; only confirmed browser
    # observations are structured JSON from the outbox worker.
    evidence = {}
    if item["state"] == "confirmed":
        try:
            evidence = json.loads(item.get("evidence") or "{}")
        except (TypeError, ValueError):
            evidence = {}
    return LiveSendResult(
        status="confirmed" if item["state"] == "confirmed" else item["state"],
        reason="" if item["state"] == "confirmed" else (item.get("evidence") or ""),
        thread_id=thread_id,
        outbox_state=item["state"],
        attempts=int(item["attempts"]),
        confirmed_event_id=event_id,
        matched_position=evidence.get("matched_position"),
        stopped_reason=report.stopped_reason,
    )

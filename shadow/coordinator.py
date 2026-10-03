"""Coordinator — the one thing that decides what happens in a tick (C01-C11, D06).

A library, not a loop. `startup()`, `tick_discover()` and `tick_due()` are the
whole surface a runner needs, so the sequence and its refusals can be tested
directly instead of inferred from a running process. The owner-control methods
at the bottom are what P04's Telegram layer will call; they exist now so the
authority boundary is in this file rather than invented later.

The rules this implements, and the reason each one is here rather than in the
browser or the model:

* **A challenge pauses everything.** Any `ReadStatus` in
  `contracts.GLOBAL_PAUSE_STATUSES` stops the tick, records a pause reason and
  raises one urgent alert. There is no path that continues reading a DOM that
  just told us it is not the DOM we thought it was.
* **`dom_unknown` is not an empty inbox.** It skips the step and alerts once an
  hour, so a renamed selector cannot look like a quiet night.
* **Every navigation is budgeted before it happens.** The budget is consumed
  first; an exhausted budget stops discovery with one action alert rather than
  letting the tab wander.
* **A preview that proves no reply is needed is recorded, not opened.** That is
  the only way to avoid burning the owner's unread badges (TRD presence rules).
  An ambiguous row is held, never guessed.
* **The first read of a thread is history, not work.** A baseline stores what it
  saw, creates no reply work, and reports trailing contact messages once as an
  info alert. Shadow does not answer a conversation it has not read.
* **Identity and the caps are re-checked at the last possible moment.** The
  check callback the outbox worker awaits before typing, before Enter and
  between items re-reads pause, mode, active hours, takeover and the item's
  cancellation generation. A draft decided ten minutes ago is not evidence now.
* **Model work cannot block a pause.** Every decision call runs under a timeout;
  a timeout holds that one thread and the tick continues.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shadow import alignment as A
from shadow import outbox as OB
from shadow import pacing
from shadow import storage as ST
from shadow.contracts import (
    GLOBAL_PAUSE_STATUSES,
    BrowserPort,
    ContextEvent,
    DecisionInput,
    DecisionPort,
    InboxRow,
    ReadResult,
)
from shadow.pacing import Clock, SystemClock
from shadow.settings import Settings
from shadow.storage import Storage

# Seconds a model role call may take before the thread is held instead. The TRD
# requires model work to be bounded and unable to block a pause, and this is the
# bound; there is no settings-table row for it yet.
ROLE_CALL_TIMEOUT = 30.0  # PROPOSED SETTING: role_call_timeout

# How many stored events are offered to alignment as the overlap tail. Bounded
# so a long thread cannot make every read quadratic, and large enough that a
# real overlap is found.
ALIGN_TAIL_EVENTS = 200  # PROPOSED SETTING: align_tail_events

# The seconds-resolution bucket used to rate-limit a repeated `dom_unknown`
# alert to once an hour.
DOM_UNKNOWN_ALERT_SECONDS = 3600  # PROPOSED SETTING: dom_unknown_alert_window

WORK_STATES_DUE = ("due",)

PREVIEW_ONLY = ("owner_last", "reaction_to_owner")


def _label_hash(label: str | None) -> str:
    """A short stable digest of a display label.

    Alerts are deduped on this instead of the label, so the database and the
    alert list never hold a contact's name (TRD diagnostics rules) while a
    repeated row still collapses to one alert.
    """
    return hashlib.sha256((label or "").encode("utf-8")).hexdigest()[:12]


@dataclass
class TickReport:
    """What one tick did. Counts and reasons only, never text."""

    rows: int = 0
    opened: int = 0
    preview_only: int = 0
    held: int = 0
    ingested: int = 0
    assessed: int = 0
    drafted: int = 0
    sent: int = 0
    stopped_reason: str = ""
    notes: list[str] = field(default_factory=list)

    def note(self, reason: str) -> None:
        self.notes.append(reason)


class DryRunBrowser:
    """Wraps a real BrowserPort so a dry run cannot send.

    Reads pass through, because a dry run is allowed to look; `send_one` raises
    instead of typing. The coordinator refuses to build one in any other mode.
    """

    def __init__(self, inner: BrowserPort) -> None:
        self._inner = inner

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if name == "send_one":
            return self._send_one
        return attribute

    async def _send_one(self, thread_id, text, check):
        raise RuntimeError(
            "dry run: send_one was called on a dry-run browser wrapper. A dry run "
            "must never type; this is a bug in the coordinator's mode handling."
        )


class Coordinator:
    def __init__(
        self,
        storage: Storage,
        browser: BrowserPort,
        decisions: DecisionPort,
        settings: Settings,
        *,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        account_id: str = "",
        dry_run: bool = False,
    ) -> None:
        self.storage = storage
        self.browser = DryRunBrowser(browser) if dry_run else browser
        self.decisions = decisions
        self.settings = settings
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()
        self.dry_run = dry_run
        self.account_id = account_id
        if not self.account_id:
            self.account_id = self._stored_account_id()
        self._dom_unknown_last_alert: str | None = None

    # --- runtime state ----------------------------------------------
    def _stored_account_id(self) -> str:
        runtime = self.storage.get_runtime()
        if runtime and runtime.get("account_id"):
            return runtime["account_id"]
        if not self.account_id:
            raise ValueError("account_id is required before the first observation")
        self.storage.set_runtime(
            account_id=self.account_id, mode=self.settings.mode,
            autonomy_scope=self._scope_text(), pause_reason=None, config_digest=None,
        )
        return self.account_id

    def _scope_text(self) -> str:
        scope = self.settings.autonomy_scope
        return scope if isinstance(scope, str) else ",".join(scope)

    @property
    def mode(self) -> str:
        if self.dry_run:
            return "observe"
        runtime = self.storage.get_runtime() or {}
        return runtime.get("mode") or self.settings.mode

    @property
    def paused(self) -> bool:
        runtime = self.storage.get_runtime() or {}
        return bool(runtime.get("pause_reason"))

    def pause(self, reason: str) -> None:
        """Owner or system pause. Persisted, so it survives a restart."""
        runtime = self.storage.get_runtime() or {}
        self.storage.set_runtime(
            account_id=runtime.get("account_id", self.account_id),
            mode=runtime.get("mode", self.settings.mode),
            autonomy_scope=runtime.get("autonomy_scope", self._scope_text()),
            pause_reason=reason,
            config_digest=runtime.get("config_digest"),
        )

    def resume(self) -> None:
        """Clears a pause. Only the owner may call this; it never clears
        identity, uncertainty or a challenge that is still on screen."""
        runtime = self.storage.get_runtime() or {}
        self.storage.set_runtime(
            account_id=runtime.get("account_id", self.account_id),
            mode=runtime.get("mode", self.settings.mode),
            autonomy_scope=runtime.get("autonomy_scope", self._scope_text()),
            pause_reason=None,
            config_digest=runtime.get("config_digest"),
        )

    def set_mode(self, mode: str) -> None:
        """Owner-only mode change. Invalidates queued autonomous drafts, because
        a draft formed under another mode's policy must not be sent under this
        one (TRD "Operating modes")."""
        runtime = self.storage.get_runtime() or {}
        self.storage.set_runtime(
            account_id=runtime.get("account_id", self.account_id),
            mode=mode,
            autonomy_scope=runtime.get("autonomy_scope", self._scope_text()),
            pause_reason=runtime.get("pause_reason"),
            config_digest=runtime.get("config_digest"),
        )
        if mode != "autonomous":
            for work in self.storage.work_in_states(("due", "drafted", "awaiting_approval")):
                self.storage.cancel_outbox_items(
                    work["conversation_id"], f"mode_changed_to_{mode}"
                )
                self.storage.cancel_decisions(
                    work["conversation_id"],
                    ("recorded", "awaiting_approval", "approved", "queued"),
                )
                self.storage.update_work(
                    work["conversation_id"], state="held", held_reason="mode_changed"
                )

    def in_autonomy_scope(self, conversation: dict) -> bool:
        scope = self.settings.autonomy_scope
        if scope == "none":
            return False
        if scope == "known":
            return conversation["contact_status"] in ("known", "approved_unknown")
        names = [str(item).strip() for item in scope]
        label = (conversation.get("label") or "").strip()
        thread_id = str(conversation.get("thread_id") or "")
        return label in names or thread_id in names

    # --- startup ---------------------------------------------------
    async def startup(self) -> TickReport:
        """Reconcile, then read the mode. Never resends anything."""
        report = TickReport()
        moved = OB.recover(self.storage)
        if moved:
            report.note(f"reconciled {moved} interrupted attempt(s) to held")
        runtime = self.storage.get_runtime()
        if runtime is None:
            self._stored_account_id()
            report.note("created the runtime row")
        if runtime and runtime.get("pause_reason"):
            report.note(f"still paused: {runtime['pause_reason']}")
        return report

    # --- navigation budget ----------------------------------------
    def _navigations_left(self) -> bool:
        return self.storage.try_consume_rates(
            "navigation", self.settings.navigation_budget, self.clock.now()
        )

    def _charge_extra_navigations(self, used: int) -> None:
        """Count what the adapter says it spent beyond the one this call
        budgeted. A cap that only counted the coordinator's own calls would
        under-count the real footprint."""
        extra = max(0, int(used or 0) - 1)
        for _ in range(extra):
            self.storage.try_consume_rates(
                "navigation", self.settings.navigation_budget, self.clock.now()
            )

    # --- discovery -------------------------------------------------
    async def tick_discover(self) -> TickReport:
        report = TickReport()
        if self.paused:
            report.stopped_reason = self._pause_reason()
            return report
        result = await self.browser.inbox()
        if result.status in GLOBAL_PAUSE_STATUSES:
            self._global_pause(result.status)
            report.stopped_reason = result.status
            report.note("global pause: the browser reported a screen that stops all sending")
            return report
        if result.status == "dom_unknown":
            self._alert_dom_unknown()
            report.stopped_reason = "dom_unknown"
            report.note("thread list not recognised: skipping, not treating as empty")
            return report
        if result.status != "ok":
            report.stopped_reason = result.status
            report.note(f"inbox read returned {result.status}")
            return report
        report.rows = len(result.rows)
        for row in result.rows:
            if report.stopped_reason:
                break
            await self._handle_row(row, report)
        if not report.stopped_reason:
            await self._assess_pending(report)
        return report

    async def _assess_pending(self, report: TickReport) -> None:
        """Assess every thread waiting for it.

        Each call carries its own timeout, so one model that never answers holds
        exactly one thread: the others are still assessed and the tick still
        returns, which is what keeps a pause processable (C02).
        """
        for work in self.storage.work_in_states(("pending",)):
            if self.paused:
                report.note("paused: no assessment this tick")
                return
            await self._assess(work["conversation_id"], report)

    def _global_pause(self, status: str) -> None:
        runtime = self.storage.get_runtime() or {}
        if runtime.get("pause_reason") != status:
            self.pause(status)
        self.storage.raise_alert(
            dedupe_key=f"global_pause:{status}", tier="urgent", reason=status
        )

    def _alert_dom_unknown(self) -> None:
        """At most one action alert per window, so a renamed selector does not
        produce a notification every tick."""
        bucket = int(self.clock.now().timestamp()) // DOM_UNKNOWN_ALERT_SECONDS
        key = f"dom_unknown:{bucket}"
        if self._dom_unknown_last_alert == key:
            return
        self._dom_unknown_last_alert = key
        self.storage.raise_alert(dedupe_key=key, tier="action", reason="dom_unknown")

    async def _handle_row(self, row: InboxRow, report: TickReport) -> None:
        if row.preview_class in PREVIEW_ONLY:
            # The preview alone proves no reply is needed. Record it and leave
            # the thread unread on the owner's phone.
            self.storage.raise_alert(
                dedupe_key=f"preview_only:{_label_hash(row.label)}",
                tier="info",
                reason=row.preview_class,
            )
            report.preview_only += 1
            return
        if not row.label_is_unique:
            self.storage.raise_alert(
                dedupe_key=f"ambiguous_row:{_label_hash(row.label)}",
                tier="action",
                reason="ambiguous_identity",
            )
            report.held += 1
            return
        if not self._navigations_left():
            self.storage.raise_alert(
                dedupe_key="navigation_budget_exhausted", tier="action",
                reason="navigation_budget_exhausted",
            )
            report.stopped_reason = "navigation_budget_exhausted"
            report.note("navigation budget exhausted: discovery stopped for this tick")
            return
        result = await self.browser.open_row(row)
        report.opened += 1
        self._charge_extra_navigations(result.navigations_used)
        await self._handle_read(result, report, label=row.label)

    async def _handle_read(
        self, result: ReadResult, report: TickReport, *, label: str | None = None
    ) -> None:
        if result.status in GLOBAL_PAUSE_STATUSES:
            self._global_pause(result.status)
            report.stopped_reason = result.status
            return
        if result.status != "ok":
            if result.status == "dom_unknown":
                self._alert_dom_unknown()
            elif result.status in ("ambiguous_identity", "wrong_thread", "wrong_account"):
                self.storage.raise_alert(
                    dedupe_key=f"read_{result.status}:{_label_hash(label)}",
                    tier="action",
                    reason=result.status,
                )
                report.held += 1
            else:
                report.note(f"read returned {result.status}")
            return
        if not result.thread_id:
            self.storage.raise_alert(
                dedupe_key=f"read_no_thread:{_label_hash(label)}", tier="action",
                reason="ambiguous_identity",
            )
            report.held += 1
            return

        conversation_id = self._conversation(result.thread_id, label, report)
        if conversation_id is None:
            return
        conversation = self.storage.get_conversation(conversation_id)

        if result.composer_text:
            # The owner typed something in the automation tab. Hold rather than
            # overwrite it (TRD "Owner takeover").
            self._hold(conversation_id, "manual_text", report, tier="info")
            return
        if result.is_group:
            self._hold(conversation_id, "group", report, tier="info")
            return
        if conversation["contact_status"] == "excluded":
            # Never read further, never assess: `/ignore` is a hard stop.
            report.note(f"conversation {conversation_id} is excluded")
            return
        if self._owner_active(conversation):
            self._hold(conversation_id, "owner_active", report, tier=None)
            return

        await self._ingest(conversation_id, result, report)

    def _conversation(self, thread_id: str, label: str | None, report: TickReport) -> int | None:
        existing = self.storage.get_conversation_by_thread(self.account_id, thread_id)
        conversation_id = self.storage.get_or_create_conversation(
            self.account_id, thread_id, label
        )
        if self.storage.get_work(conversation_id) is None:
            self.storage.set_work(conversation_id, inbound_revision=0, state="idle")
        if existing is None:
            # A thread seen for the first time is an unknown contact in every
            # mode, and the owner is told once (D22).
            self.storage.raise_alert(
                dedupe_key=f"unknown_contact:{conversation_id}",
                tier="action",
                reason="unknown_contact",
                conversation_id=conversation_id,
            )
            report.note(f"new conversation {conversation_id} held as unknown")
        return conversation_id

    def _owner_active(self, conversation: dict) -> bool:
        until = ST.parse_iso(conversation.get("owner_active_until"))
        if until is None:
            return False
        return self.clock.now() < until

    def _hold(
        self, conversation_id: int, reason: str, report: TickReport, *, tier: str | None
    ) -> None:
        self.storage.set_paused_reason(conversation_id, reason)
        work = self.storage.get_work(conversation_id)
        if work is not None:
            self.storage.update_work(conversation_id, state="held", held_reason=reason)
        if tier:
            self.storage.raise_alert(
                dedupe_key=f"hold_{reason}:{conversation_id}", tier=tier, reason=reason,
                conversation_id=conversation_id,
            )
        report.held += 1

    # --- ingestion and alignment -----------------------------------
    async def _ingest(self, conversation_id: int, result: ReadResult, report: TickReport) -> None:
        stored = self.storage.get_events(conversation_id)
        conversation = self.storage.get_conversation(conversation_id)
        window = A.keys_of(result.events)
        if not conversation["first_seen_baseline_done"]:
            self._record_baseline(conversation_id, result, report)
            return
        result_align = A.align(A.keys_of(stored[-ALIGN_TAIL_EVENTS:]), window)
        if result_align.status in ("gap", "ambiguous"):
            # No work is created from a window that cannot be placed: storing it
            # would invent or drop messages.
            self._hold(
                conversation_id,
                f"align_{result_align.status}",
                report,
                tier="action",
            )
            report.note(f"conversation {conversation_id}: {result_align.status}")
            return
        if not result_align.is_new:
            return
        new_keys = result_align.new_events(window)
        new_events = result.events[result_align.new_start_index:]
        self._store_events(conversation_id, new_events, stored)
        report.ingested += len(new_events)
        await self._after_new_events(conversation_id, new_events, report)

    def _record_baseline(
        self, conversation_id: int, result: ReadResult, report: TickReport
    ) -> None:
        """The first read is history. It creates no reply work, ever: Shadow does
        not answer a conversation it has not watched (D40)."""
        self._store_events(conversation_id, result.events, [])
        self.storage.mark_baseline_done(conversation_id)
        report.ingested += len(result.events)
        trailing = A.trailing_unanswered_count(A.keys_of(result.events), 0)
        if trailing:
            self.storage.raise_alert(
                dedupe_key=f"baseline_unanswered:{conversation_id}",
                tier="info",
                reason="baseline_unanswered",
                conversation_id=conversation_id,
            )
        report.note(f"conversation {conversation_id} baselined ({len(result.events)} events)")

    def _store_events(
        self, conversation_id: int, events, stored: list[dict]
    ) -> None:
        """Append the window's new events.

        Origin is what it can be proven to be: a contact message seen in the DOM
        is the contact's; an owner message seen in the DOM is the owner's only
        if it does not match a confirmed send of ours, which `_after_new_events`
        decides. Everything else stays `unknown`, which is the lesson of D33.
        """
        confirmed = self._confirmed_texts(conversation_id)
        for event in events:
            # A contact message is the contact's, with no claim about the owner.
            # An owner message is only ours if it matches a confirmed send.
            origin = "unknown"
            if event.sender == "owner":
                origin = (
                    "shadow_generated"
                    if A.normalize_text(event.text) in confirmed
                    else "owner_authored"
                )
            self.storage.add_event(
                conversation_id=conversation_id,
                sender=event.sender,
                kind=event.kind,
                text=event.text,
                observed_at=ST.iso(self.clock.now()),
                platform_ts=None,
                reply_to=event.reply_to_text,
                source="dom",
                origin=origin,
                seq=None,
            )

    def _confirmed_texts(self, conversation_id: int) -> set[str]:
        return {
            A.normalize_text(item["text"])
            for item in self.storage.outbox_items(conversation_id)
            if item["state"] == "confirmed"
        }

    async def _after_new_events(
        self, conversation_id: int, events, report: TickReport
    ) -> None:
        owner_events = [e for e in events if e.sender == "owner"]
        contact_events = [e for e in events if e.sender == "contact"]
        confirmed = self._confirmed_texts(conversation_id)
        for event in owner_events:
            if A.normalize_text(event.text) in confirmed:
                continue
            await self._takeover(conversation_id, report)
        if contact_events:
            await self._queue_work(conversation_id, contact_events, report)

    async def _takeover(self, conversation_id: int, report: TickReport) -> None:
        """An owner message we did not send. Pending work for that thread is
        cancelled and drafting stops until `takeover_window` passes or the owner
        resumes the contact (D42)."""
        until = self.clock.now() + timedelta(seconds=self.settings.takeover_window)
        self.storage.set_owner_active_until(conversation_id, ST.iso(until))
        self.storage.cancel_outbox_items(conversation_id, "owner_takeover")
        self.storage.cancel_decisions(
            conversation_id, ("recorded", "awaiting_approval", "approved", "queued")
        )
        work = self.storage.get_work(conversation_id)
        if work is not None:
            self.storage.update_work(
                conversation_id,
                state="owner_active",
                held_reason="owner_takeover",
                cancel_generation=work["cancel_generation"] + 1,
            )
        report.note(f"conversation {conversation_id}: owner takeover")

    async def _queue_work(
        self, conversation_id: int, events, report: TickReport
    ) -> None:
        conversation = self.storage.get_conversation(conversation_id)
        if conversation["contact_status"] == "excluded":
            return
        now = self.clock.now()
        work = self.storage.get_work(conversation_id)
        revision = 0 if work is None else int(work["inbound_revision"]) + 1
        first_pending = now if work is None or not work["first_pending_at"] else ST.parse_iso(
            work["first_pending_at"]
        )
        last_inbound = now
        momentum = self._momentum(conversation_id, now)
        delay = pacing.sample_delay(
            self.settings,
            momentum=momentum,
            profile=self.storage.latency_profile() or None,
            local_hour=pacing.local_hour(now, self.settings),
            rng=self.rng,
        )
        due = pacing.due_time(
            first_pending_at=first_pending or now,
            last_inbound_at=last_inbound,
            sampled_delay=delay,
            settings=self.settings,
        )
        if self.settings.active_hours and self.settings.timezone:
            due, _ = pacing.apply_active_hours(
                due, self.settings, rng=self.rng, profile=self.storage.latency_profile() or None
            )
        if work is None:
            self.storage.set_work(
                conversation_id,
                inbound_revision=revision,
                due_at=ST.iso(due),
                first_pending_at=ST.iso(first_pending or now),
                state="pending",
                cancel_generation=0,
            )
        else:
            # A newer revision invalidates older drafts and items: they were
            # decided against context that has now changed.
            if work["state"] in ("drafted", "awaiting_approval", "due"):
                self.storage.cancel_outbox_items(conversation_id, "newer_revision")
                self.storage.cancel_decisions(
                    conversation_id, ("recorded", "awaiting_approval", "approved", "queued")
                )
            self.storage.update_work(
                conversation_id,
                inbound_revision=revision,
                due_at=ST.iso(due),
                first_pending_at=work["first_pending_at"] or ST.iso(first_pending or now),
                state="pending",
                last_inbound_at=ST.iso(last_inbound),
                sampled_delay_seconds=int(delay),
                held_reason=None,
            )
        self.storage.set_paused_reason(conversation_id, None)
        report.note(f"conversation {conversation_id}: work queued (revision {revision})")

    def _momentum(self, conversation_id: int, now: datetime) -> bool:
        last = None
        for event in reversed(self.storage.get_events(conversation_id)):
            if event["sender"] == "owner":
                last = ST.parse_iso(event["observed_at"])
                break
        return pacing.is_momentum(now, last, self.settings)

    # --- assessment ------------------------------------------------
    async def _assess(self, conversation_id: int, report: TickReport) -> None:
        work = self.storage.get_work(conversation_id)
        if work is None or work["state"] != "pending":
            return
        conversation = self.storage.get_conversation(conversation_id)
        if conversation["contact_status"] == "excluded":
            return
        events = self.storage.get_events(conversation_id)
        batch = [
            e for e in events
            if e["source"] == "dom" and e["sender"] == "contact"
        ][-self._batch_size():]
        if not batch:
            self.storage.update_work(conversation_id, state="idle", held_reason=None)
            return
        self.storage.update_work(conversation_id, state="assessing")
        data = DecisionInput(
            conversation_id=conversation_id,
            contact_status=conversation["contact_status"],
            relationship={},
            owner_profile={},
            current_batch=tuple(
                ContextEvent(e["id"], e["sender"], e["kind"], e["text"] or "") for e in batch
            ),
            recent=tuple(
                ContextEvent(e["id"], e["sender"], e["kind"], e["text"] or "")
                for e in events
                if e["id"] not in {b["id"] for b in batch}
            ),
        )
        report.assessed += 1
        assessed = await self._call(self.decisions.assess(data), conversation_id, "assess")
        if assessed is None:
            self._hold(conversation_id, "assessment_timeout", report, tier="action")
            return
        if assessed.failure is not None:
            self._hold(conversation_id, f"assessment_{assessed.failure}", report, tier="action")
            return
        assessment = assessed.value
        decision_id = self._record_decision(conversation_id, work, "recorded", {
            "assessment": {
                "risk": assessment.risk,
                "reason_code": assessment.reason_code,
                "action_hint": assessment.action_hint,
                "signals": sorted(assessment.signals),
                "evidence_event_ids": list(assessment.evidence_event_ids),
            }
        })
        if assessment.action_hint == "urgent":
            self.storage.raise_alert(
                dedupe_key=f"risk_{assessment.risk}:{conversation_id}:{work['inbound_revision']}",
                tier="urgent",
                reason=assessment.reason_code,
                conversation_id=conversation_id,
            )
            self.storage.update_work(conversation_id, state="held", held_reason="risk_urgent")
            return
        if assessment.action_hint == "hold" or assessment.signals:
            # A confusion or automation-question signal is a hold, not a
            # judgement call in either direction.
            self.storage.raise_alert(
                dedupe_key=f"risk_hold:{conversation_id}:{work['inbound_revision']}",
                tier="action",
                reason=assessment.reason_code,
                conversation_id=conversation_id,
            )
            self.storage.update_work(
                conversation_id, state="held", held_reason=f"risk_{assessment.reason_code}"
            )
            return
        drafted = await self._call(
            self.decisions.draft(data, assessment), conversation_id, "draft"
        )
        if drafted is None:
            self._hold(conversation_id, "draft_timeout", report, tier="action")
            return
        if drafted.failure is not None:
            self._hold(conversation_id, f"draft_{drafted.failure}", report, tier="action")
            return
        reply = drafted.value
        if reply.action == "skip" or not reply.messages:
            self.storage.set_decision_status(decision_id, "done")
            self.storage.update_work(conversation_id, state="idle", held_reason=None)
            report.drafted += 1
            return
        report.drafted += 1
        self._queue_draft(conversation_id, work, decision_id, reply.messages, report)

    def _batch_size(self) -> int:
        # The current batch is what the contact said since Shadow last spoke.
        # Bounded by the burst ceiling, which is also the largest number of
        # messages one decision may cover.
        return self.settings.burst_max_items

    def _record_decision(
        self, conversation_id: int, work: dict, status: str, record: dict
    ) -> int:
        return self.storage.add_decision(
            conversation_id=conversation_id,
            source_revision=int(work["inbound_revision"]),
            record_json=json.dumps(record, sort_keys=True),
            versions_json="{}",
            status=status,
        )

    def _queue_draft(
        self, conversation_id: int, work: dict, decision_id: int, messages, report: TickReport
    ) -> None:
        mode = self.mode
        conversation = self.storage.get_conversation(conversation_id)
        status = conversation["contact_status"]
        if status in ("unknown_pending",):
            # Held in every mode until the owner approves the contact (D22).
            self.storage.set_decision_status(decision_id, "recorded")
            self.storage.update_work(conversation_id, state="held", held_reason="unknown_contact")
            return
        if status == "excluded":
            self.storage.set_decision_status(decision_id, "recorded")
            self.storage.update_work(conversation_id, state="idle", held_reason=None)
            return
        if mode == "observe":
            # observe records the decision and stops. It never creates an outbox
            # item except for an explicit owner command.
            self.storage.set_decision_status(decision_id, "recorded")
            self.storage.update_work(conversation_id, state="idle", held_reason=None)
            return
        autonomous = (
            mode == "autonomous"
            and status == "known"
            and self.in_autonomy_scope(conversation)
        )
        if not autonomous:
            # assisted, and autonomous-out-of-scope, both wait for an approval
            # card. P04 delivers it; the decision is already durable.
            self.storage.set_decision_status(decision_id, "awaiting_approval")
            self.storage.update_work(
                conversation_id, state="awaiting_approval", held_reason=None
            )
            return
        for index, message in enumerate(messages[: self.settings.burst_max_items]):
            self.storage.add_outbox_item(
                decision_id=decision_id,
                item_index=index,
                conversation_id=conversation_id,
                text=str(message)[: self.settings.bubble_max_chars],
                authority="autonomous",
                revision=int(work["inbound_revision"]),
            )
        self.storage.set_decision_status(decision_id, "queued")
        self.storage.update_work(conversation_id, state="due", held_reason=None)

    async def _call(self, awaitable, conversation_id: int, what: str):
        """Run one model call under a timeout.

        A model that hangs must not be able to hold a pause hostage, so the wait
        is bounded and a timeout holds exactly one thread.
        """
        try:
            return await asyncio.wait_for(awaitable, timeout=ROLE_CALL_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        except Exception as exc:  # noqa: BLE001 - any provider failure is a hold
            self.storage.raise_alert(
                dedupe_key=f"decision_{what}_error:{conversation_id}:{type(exc).__name__}",
                tier="action",
                reason=f"decision_{what}_error",
                conversation_id=conversation_id,
            )
            return None

    # --- due work --------------------------------------------------
    async def tick_due(self) -> TickReport:
        report = TickReport()
        now = self.clock.now()
        for work in self.storage.work_in_states(WORK_STATES_DUE):
            due = ST.parse_iso(work["due_at"])
            if due is None or due > now:
                continue
            await self._run_due(work, report)
        return report

    async def _run_due(self, work: dict, report: TickReport) -> None:
        conversation_id = work["conversation_id"]
        conversation = self.storage.get_conversation(conversation_id)
        if conversation["contact_status"] == "excluded":
            self.storage.update_work(conversation_id, state="idle", held_reason=None)
            return
        if self._owner_active(conversation):
            self._hold(conversation_id, "owner_active", report, tier=None)
            return
        first_pending = ST.parse_iso(work["first_pending_at"])
        if first_pending is not None and (
            now_stale(self.clock.now(), first_pending, self.settings)
        ):
            # Stale work is reassessed against a fresh read, never replayed.
            self.storage.cancel_outbox_items(conversation_id, "stale_reeval")
            self.storage.cancel_decisions(
                conversation_id, ("recorded", "awaiting_approval", "approved", "queued")
            )
            self.storage.update_work(
                conversation_id, state="pending", held_reason="stale_reeval"
            )
            report.note(f"conversation {conversation_id}: stale, reassessing")
            return
        if not self.pacing_allows_send():
            report.stopped_reason = report.stopped_reason or "outside_active_hours"
            report.note("outside active hours: nothing due is sent")
            return
        if self.paused:
            report.stopped_reason = report.stopped_reason or self._pause_reason()
            return
        if self.mode == "observe":
            report.note("observe mode: no sends")
            return
        if not self.caps_available(conversation_id):
            report.stopped_reason = report.stopped_reason or "send_cap"
            self.storage.raise_alert(
                dedupe_key="send_cap_exhausted", tier="action", reason="send_cap_exhausted"
            )
            return

        result = await self.browser.read_thread(conversation["thread_id"])
        if result.status in GLOBAL_PAUSE_STATUSES:
            self._global_pause(result.status)
            report.stopped_reason = result.status
            return
        if result.status != "ok":
            report.note(f"due re-read returned {result.status}")
            self.storage.update_work(
                conversation_id, state="held", held_reason=f"reread_{result.status}"
            )
            return
        stored = self.storage.get_events(conversation_id)
        result_align = A.align(A.keys_of(stored[-ALIGN_TAIL_EVENTS:]), A.keys_of(result.events))
        if result_align.status == "ok" and result_align.new_start_index < len(result.events):
            # The contact said something after the draft was formed. The draft is
            # cancelled and the thread goes back to assessment.
            new_events = result.events[result_align.new_start_index:]
            self._store_events(conversation_id, new_events, stored)
            self.storage.cancel_outbox_items(conversation_id, "new_inbound_before_send")
            self.storage.cancel_decisions(
                conversation_id, ("recorded", "awaiting_approval", "approved", "queued")
            )
            owner_new = [e for e in new_events if e.sender == "owner"]
            if owner_new:
                await self._takeover(conversation_id, report)
            else:
                self.storage.update_work(
                    conversation_id, state="pending", held_reason="new_inbound"
                )
            report.note(f"conversation {conversation_id}: reassessed after new inbound")
            return
        if result_align.status != "ok":
            self._hold(conversation_id, f"align_{result_align.status}", report, tier="action")
            return
        if result.composer_text:
            self._hold(conversation_id, "manual_text", report, tier="info")
            return

        items = [
            item for item in self.storage.outbox_items(conversation_id)
            if item["state"] == "pending"
        ]
        if not items:
            self.storage.update_work(conversation_id, state="idle", held_reason=None)
            return
        self.storage.update_work(conversation_id, state="sending")
        send_report = await OB.send_items(
            self.storage, self.browser, conversation, items,
            self._check_for(conversation_id), self.clock,
        )
        report.sent += len(send_report.confirmed)
        if send_report.stopped_reason:
            report.stopped_reason = send_report.stopped_reason
        if not send_report.uncertain and not send_report.failed:
            self.storage.update_work(conversation_id, state="idle", held_reason=None)
            for decision in self.storage.decisions_for(conversation_id):
                if decision["status"] in ("queued",):
                    self.storage.set_decision_status(decision["id"], "done")

    def _pause_reason(self) -> str:
        runtime = self.storage.get_runtime() or {}
        return runtime.get("pause_reason") or "paused"

    def pacing_allows_send(self) -> bool:
        if not self.settings.active_hours or not self.settings.timezone:
            # No schedule configured: `validate_for_mode` already refuses any
            # sending mode, so reaching here means observe, which does not send.
            return self.mode == "observe"
        return pacing.in_active_hours(self.clock.now(), self.settings)

    def caps_allow(self, conversation_id: int) -> bool:
        """Reserve one message attempt from both caps in one transaction."""
        now = self.clock.now()
        thread_kind = f"send_thread:{conversation_id}"
        with self.storage.transaction():
            if not self.storage.rates_available(
                "send_account", self.settings.send_cap_account, now
            ) or not self.storage.rates_available(
                thread_kind, self.settings.send_cap_thread, now
            ):
                return False
            if not self.storage.try_consume_rates(
                "send_account", self.settings.send_cap_account, now
            ) or not self.storage.try_consume_rates(
                thread_kind, self.settings.send_cap_thread, now
            ):
                raise ST.StorageError("send cap changed during reservation")
            return True

    def caps_available(self, conversation_id: int) -> bool:
        """Whether a send would fit inside both caps right now. Reads only."""
        now = self.clock.now()
        return self.storage.rates_available(
            "send_account", self.settings.send_cap_account, now
        ) and self.storage.rates_available(
            f"send_thread:{conversation_id}", self.settings.send_cap_thread, now
        )

    def _check_for(self, conversation_id: int):
        """The check the outbox worker awaits before typing, before Enter and
        between items. Everything that can change while a burst is running is
        re-read here, at the last possible moment."""
        generation = self._generation(conversation_id)

        async def check(point: str) -> bool:
            if self.paused:
                return False
            if self.mode == "observe":
                return False
            if not self.pacing_allows_send():
                return False
            if self._generation(conversation_id) != generation:
                return False
            conversation = self.storage.get_conversation(conversation_id)
            if conversation is None or conversation["contact_status"] == "excluded":
                return False
            if self._owner_active(conversation):
                return False
            return (self.caps_allow(conversation_id) if point == "before_enter"
                    else self.caps_available(conversation_id))

        return check

    def _generation(self, conversation_id: int) -> int:
        work = self.storage.get_work(conversation_id)
        return 0 if work is None else int(work["cancel_generation"])

    # --- owner controls (P04 calls these) ---------------------------
    def approve_contact(self, conversation_id: int) -> None:
        """`/approve`: the contact becomes `approved_unknown`. It permits cautious
        drafts and never imported relationship facts (D22)."""
        conversation = self.storage.get_conversation(conversation_id)
        if conversation is None:
            raise ST.StorageError(f"conversation {conversation_id} does not exist")
        self.storage.set_contact_status(
            conversation_id, "approved_unknown", evidence="owner_approved"
        )
        self.storage.set_alert_state(
            self._alert_id(f"unknown_contact:{conversation_id}"), "resolved"
        )
        work = self.storage.get_work(conversation_id)
        if work is not None and work["state"] == "held" and work["held_reason"] == "unknown_contact":
            self.storage.update_work(conversation_id, state="pending", held_reason=None)

    def ignore_contact(self, conversation_id: int) -> None:
        """`/ignore`: `excluded`. From here the thread is never opened, read or
        assessed, and its queued work is cancelled."""
        conversation = self.storage.get_conversation(conversation_id)
        if conversation is None:
            raise ST.StorageError(f"conversation {conversation_id} does not exist")
        self.storage.set_contact_status(conversation_id, "excluded", evidence="owner_ignored")
        self.storage.cancel_outbox_items(conversation_id, "contact_excluded")
        self.storage.cancel_decisions(
            conversation_id, ("recorded", "awaiting_approval", "approved", "queued")
        )
        self.storage.set_paused_reason(conversation_id, "excluded")
        work = self.storage.get_work(conversation_id)
        if work is not None:
            self.storage.update_work(
                conversation_id, state="idle", held_reason="contact_excluded",
                cancel_generation=work["cancel_generation"] + 1,
            )

    def bootstrap_known(self, conversation_ids) -> int:
        """Owner-invoked bulk mark. The only way a contact becomes `known`
        without Shadow having observed anything about them, and it is explicit."""
        marked = 0
        for conversation_id in conversation_ids:
            conversation = self.storage.get_conversation(conversation_id)
            if conversation is None:
                continue
            self.storage.set_contact_status(
                conversation_id, "known", evidence="owner_bootstrap"
            )
            marked += 1
        return marked

    def resume_contact(self, conversation_id: int) -> None:
        """`/resume <contact>`: ends a takeover window early."""
        self.storage.set_owner_active_until(conversation_id, None)
        work = self.storage.get_work(conversation_id)
        if work is not None and work["state"] == "owner_active":
            self.storage.update_work(
                conversation_id, state="pending", held_reason=None,
                cancel_generation=work["cancel_generation"] + 1,
            )

    def enqueue_owner_send(self, conversation_id: int, text: str) -> tuple[int, int]:
        return OB.enqueue_owner_send(
            self.storage, conversation_id, text, now=self.clock.now(),
            revision=self._generation(conversation_id),
        )

    def _alert_id(self, dedupe_key: str) -> int:
        for alert in self.storage.alerts():
            if alert["dedupe_key"] == dedupe_key:
                return alert["id"]
        return 0

    # --- polling and dry run ---------------------------------------
    def poll_interval(self) -> int:
        return pacing.poll_interval(self.clock.now(), self.settings)

    @staticmethod
    def dry_run_copy(storage: Storage) -> tuple[Storage, Path]:
        """A verified copy of the live database in a throwaway directory.

        A dry run reads the copy, so nothing it does can reach production
        reply, memory or handled state. The copy is disposable and is not a
        backup: the real one is `Storage.backup` into the data directory.
        """
        scratch = Path(tempfile.mkdtemp(prefix="shadow-dryrun-"))
        storage.backup(scratch / ST.DB_FILENAME)
        return ST.open_storage(scratch), scratch


def now_stale(now: datetime, first_pending: datetime, settings: Settings) -> bool:
    return (now - first_pending) > timedelta(seconds=settings.stale_reeval)

"""Shared test doubles for the P03 suites.

Not a test module (no `test_` prefix), imported by tests/test_outbox.py and
tests/test_coordinator.py. Everything here is fictional: thread ids are digits,
labels are "Test ...", and no text from any real conversation appears.

The doubles exist so the coordinator and the outbox worker can be tested against
the *real* production code paths - the real state machine, the real SQLite
transactions, the real alignment and pacing - while the two boundaries that
cannot exist offline (the browser and the model) are scripted. That is the point
of D36: reliability is proven here, and the real adapters are wired in later.

`FakeBrowser` is scriptable per call so a test can say "item 1 confirms, item 2
times out after Enter" and get exactly that, which is the only way to reach the
partial-burst and crash-boundary states.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shadow import contracts as C
from shadow.pacing import ManualClock

UTC = timezone.utc


# --- browser ------------------------------------------------------------


def row(
    index: int = 0,
    label: str = "Test Contact One",
    preview: str = "kal milte hai?",
    preview_class: str = "text_in",
    unread: bool = False,
    unique: bool = True,
    group: bool | None = None,
) -> C.InboxRow:
    return C.InboxRow(
        row_index=index,
        label=label,
        preview_text=preview,
        preview_class=preview_class,
        unread_hint=unread,
        label_is_unique=unique,
        group_hint=group,
    )


def event(position: int, sender: str, text: str, kind: str = "text") -> C.ObservedEvent:
    return C.ObservedEvent(position=position, sender=sender, kind=kind, text=text)


def read(
    thread_id: str = "100200300",
    events: tuple = (),
    *,
    status: str = "ok",
    is_group: bool | None = None,
    composer_text: str = "",
    participant_links: int = 1,
    complete: bool = True,
) -> C.ReadResult:
    return C.ReadResult(
        status=status,
        thread_id=thread_id,
        events=tuple(events),
        complete=complete,
        is_group=is_group,
        participant_links=participant_links,
        composer_text=composer_text,
    )


@dataclass
class SendScript:
    """One scripted `send_one` outcome.

    `raise_before` simulates a process death at a named point: "send" (the call
    never returns) or "after_send" (the adapter returned but the worker died
    before it could confirm). Both leave an `attempting` row for startup to
    reconcile, and neither may ever be resent.
    """

    result: C.SendResult | None = None
    raise_before: str | None = None
    delay: float = 0.0


class FakeBrowser:
    """A `BrowserPort` with scripted answers and a full call log.

    `check` is the coordinator callback the real adapter must honour; this one
    records the points it was called at and returns whatever `check_result` says,
    so a test can prove the worker re-checked before typing and before Enter.
    """

    def __init__(
        self,
        *,
        inbox_result: C.InboxResult | None = None,
        reads: dict | None = None,
        send_results: list | None = None,
        check_result: bool = True,
    ) -> None:
        self.inbox_result = inbox_result or C.InboxResult(status="ok", rows=())
        self.reads = reads or {}
        self.send_results = list(send_results or [])
        self.check_result = check_result
        self.calls: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.check_points: list[str] = []
        self.opened: list[C.InboxRow] = []
        self._default_read: C.ReadResult | None = None

    # --- BrowserPort -------------------------------------------------
    async def inbox(self) -> C.InboxResult:
        self.calls.append("inbox")
        return self.inbox_result

    async def open_row(self, inbox_row: C.InboxRow) -> C.ReadResult:
        self.calls.append(f"open_row:{inbox_row.row_index}")
        self.opened.append(inbox_row)
        return self._read_for(inbox_row.row_index)

    async def read_thread(self, thread_id: str, want: int = 0) -> C.ReadResult:
        self.calls.append(f"read_thread:{thread_id}")
        return self._read_for(thread_id)

    async def send_one(self, thread_id: str, text: str, check) -> C.SendResult:
        self.calls.append(f"send_one:{thread_id}")
        for point in ("before_type", "before_enter"):
            self.check_points.append(point)
            if not await check(point):
                return C.SendResult(status="failed_before_submit", thread_id=thread_id,
                                    error_code=f"check_failed_at_{point}")
        self.sent.append((thread_id, text))
        script = (
            self.send_results.pop(0)
            if self.send_results
            else SendScript(C.SendResult(status="confirmed", thread_id=thread_id))
        )
        if script.delay:
            await asyncio.sleep(script.delay)
        if script.raise_before:
            # A BaseException, because a real process death must not be caught
            # by the worker's `except Exception` around the adapter call.
            raise SimulatedCrash(f"simulated process death {script.raise_before}")
        return script.result

    # --- helpers -----------------------------------------------------
    def _read_for(self, key) -> C.ReadResult:
        thread_id = f"1002003{key:02d}" if isinstance(key, int) else key
        if thread_id in self.reads:
            scripted = self.reads[thread_id]
        elif self._default_read is not None:
            scripted = self._default_read
        else:
            scripted = read(thread_id)
        if callable(scripted):
            scripted = scripted()
        if scripted.thread_id is None:
            scripted = C.ReadResult(**{**scripted.__dict__, "thread_id": thread_id})
        return scripted

    def set_default_read(self, result: C.ReadResult | None) -> None:
        self._default_read = result

    def queue(self, result: C.SendResult) -> None:
        self.send_results.append(SendScript(result))

    def queue_crash(self, point: str = "send") -> None:
        self.send_results.append(SendScript(None, raise_before=point))


# --- decisions ----------------------------------------------------------


def risk(
    level: str = "none", hint: str = "proceed", signals=(), reason: str = "ordinary_reply"
) -> C.RiskAssessment:
    return C.RiskAssessment(
        risk=level, reason_code=reason, action_hint=hint, signals=frozenset(signals)
    )


def draft(*messages: str, action: str = "reply", reason: str = "ordinary_reply") -> C.ReplyDraft:
    return C.ReplyDraft(action=action, reason_code=reason, messages=tuple(messages))


def outcome(value=None, *, role: str = "risk", failure: str | None = None, delay: float = 0.0):
    return _ScriptedOutcome(value, role, failure, delay)


@dataclass
class _ScriptedOutcome:
    value: object
    role: str
    failure: str | None
    delay: float


class FakeDecisions:
    """A `DecisionPort` whose per-role answers are scripted.

    A `delay` larger than the coordinator's role timeout is how a test reaches
    the C02 case: one thread's model call hangs while another must still be
    assessed, and a pause must still be processed.
    """

    def __init__(self, *, assess=None, draft_outcomes=None, extract=None) -> None:
        self._assess = list(assess or [outcome(risk())])
        self._draft = list(draft_outcomes or [outcome(draft("haan, 7 pm?"))])
        self._extract = list(extract or [outcome(C.FactProposal())])
        self.calls: list[tuple[str, int]] = []

    async def assess(self, data: C.DecisionInput) -> C.RoleOutcome:
        self.calls.append(("assess", data.conversation_id))
        return self._next(self._assess, "risk", data)

    async def draft(self, data: C.DecisionInput, assessment: C.RiskAssessment) -> C.RoleOutcome:
        self.calls.append(("draft", data.conversation_id))
        return self._next(self._draft, "reply", data)

    async def extract(self, data: C.DecisionInput) -> C.RoleOutcome:
        self.calls.append(("extract", data.conversation_id))
        return self._next(self._extract, "extraction", data)

    @staticmethod
    async def _next(queue, role, data):
        scripted = queue.pop(0) if queue else _ScriptedOutcome(risk(), role, None, 0.0)
        if scripted.delay:
            await asyncio.sleep(scripted.delay)
        if scripted.failure is not None:
            return C.RoleOutcome(role=role, failure=scripted.failure)
        return C.RoleOutcome(role=role, value=scripted.value)


# --- storage doubles ----------------------------------------------------


@dataclass
class CrashyStorage:
    """Wraps a Storage and raises at a named call, to reproduce a process death
    at each crash boundary (D05) without killing a real process.

    The wrapper is deliberately thin: it delegates everything and only fails
    where it is told to, so the state left behind is the state the real code
    wrote.
    """

    inner: object
    fail_at: str = ""
    fired: bool = False
    _seen: list[str] = field(default_factory=list)

    def _maybe_fail(self, name: str) -> None:
        self._seen.append(name)
        if self.fail_at == name and not self.fired:
            self.fired = True
            raise SimulatedCrash(f"simulated process death at {name}")

    def __getattr__(self, name):
        attribute = getattr(self.inner, name)
        if not callable(attribute):
            return attribute

        def wrapper(*args, **kwargs):
            self._maybe_fail(name)
            return attribute(*args, **kwargs)

        return wrapper

    @property
    def attempted_before_crash(self) -> list[str]:
        return list(self._seen)


class SimulatedCrash(BaseException):
    """A stand-in for the process dying.

    Deliberately a BaseException, not an Exception: the worker's `except
    Exception` around the browser call exists to turn an ADAPTER FAILURE into
    `uncertain`, and a real process death must not be swallowed by it. Using an
    ordinary exception here would have tested the wrong thing and passed.
    """


__all__ = [
    "UTC",
    "FakeBrowser",
    "FakeDecisions",
    "CrashyStorage",
    "ManualClock",
    "SendScript",
    "SimulatedCrash",
    "draft",
    "event",
    "outcome",
    "read",
    "risk",
    "row",
]

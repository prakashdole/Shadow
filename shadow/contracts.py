"""Shared interfaces between the browser adapter, the coordinator and the
decision producer.

Owned by the coordinator (not by any one phase agent). Three phases build
against these types at the same time: P02 implements `BrowserPort`, P05
implements `DecisionPort`, and P03 calls both. Change this file only with a
decision in plan/decisions.md, because every side depends on it.

Values mirror plan/trd.md ("Data contracts") and the storage CHECK constraints
in shadow/storage.py. Numbers never live here; they come from shadow.settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Protocol

# --- enums (keep identical to storage CHECK constraints) ----------------

Sender = Literal["owner", "contact", "unknown"]
Kind = Literal["text", "media", "reaction", "system", "unknown"]

ReadStatus = Literal[
    "ok",                  # pane read and bound to the requested thread
    "empty_verified",      # the UI positively shows an empty thread/inbox
    "login_required",      # login page or logged-out state
    "challenge",           # checkpoint / challenge / suspended / rate-limit screen
    "receipts_visible",    # read receipts are visible: owner setting violated
    "wrong_account",       # the logged-in account is not the configured one
    "wrong_thread",        # the open thread id differs from the requested one
    "ambiguous_identity",  # row/label cannot be tied to exactly one thread
    "dom_unknown",         # expected structure missing; never treated as empty
]
READ_STATUSES: tuple[str, ...] = ReadStatus.__args__  # type: ignore[attr-defined]

# Statuses that must pause ALL sending (TRD presence rules, operations matrix).
GLOBAL_PAUSE_STATUSES = frozenset(
    {"login_required", "challenge", "receipts_visible", "wrong_account"}
)

PreviewClass = Literal[
    "owner_last",         # "You: ..." / "You sent an attachment" - no reply needed
    "reaction_to_owner",  # contact reacted to the owner's message
    "attachment_in",      # contact sent media/attachment
    "text_in",            # ordinary inbound preview
    "unknown",
]

SendStatus = Literal["confirmed", "failed_before_submit", "uncertain"]
SEND_STATUSES: tuple[str, ...] = SendStatus.__args__  # type: ignore[attr-defined]

CheckPoint = Literal["before_type", "before_enter", "between_items"]

Risk = Literal["none", "money", "commitment", "distress", "privacy", "unsupported", "uncertain"]
ActionHint = Literal["proceed", "hold", "urgent"]
Signal = Literal["confusion", "automation_question", "owner_needed"]
ReplyAction = Literal["reply", "skip"]
Role = Literal["risk", "reply", "extraction"]


# --- browser side (P02 implements, P03 consumes) -----------------------

@dataclass(frozen=True)
class ObservedEvent:
    """One item read from the message pane, oldest first by `position`.

    `position` is the order within THIS read window only; it is not an id.
    There are no stable message ids in the DOM (D31), so the coordinator aligns
    windows by sequence. Never de-duplicate on (sender, text) (D32).
    """

    position: int
    sender: Sender
    kind: Kind
    text: str
    media_label: str | None = None     # e.g. "reel", "post", "reel_unavailable"
    reply_to_text: str | None = None   # quoted text when a reply target is shown
    laid_out: bool = True              # False: no geometry, sender may be unknown
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class InboxRow:
    """One thread row as rendered. `label` is presentation, never identity."""

    row_index: int
    label: str
    preview_text: str
    preview_class: PreviewClass
    unread_hint: bool                  # inferred; Instagram does not render it (P00b Q6)
    label_is_unique: bool              # False: another row shows the same label
    group_hint: bool | None = None     # None: cannot tell from the row (P00b Q7)


@dataclass(frozen=True)
class InboxResult:
    status: ReadStatus
    rows: tuple[InboxRow, ...] = ()
    navigations_used: int = 0
    detail: str = ""


@dataclass(frozen=True)
class ReadResult:
    status: ReadStatus
    thread_id: str | None = None       # digits parsed from /direct/t/<digits>/
    events: tuple[ObservedEvent, ...] = ()
    complete: bool = False             # True only if the window reached a known anchor
    is_group: bool | None = None
    participant_links: int = 0         # header profile-link count (group evidence)
    composer_text: str = ""            # non-empty: manual text present -> hold
    navigations_used: int = 0
    detail: str = ""


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    thread_id: str | None = None
    matched_position: int | None = None   # position of the new owner bubble, if confirmed
    evidence: dict = field(default_factory=dict)
    error_code: str | None = None
    navigations_used: int = 0


# A coordinator callback. The browser MUST await it at each CheckPoint and
# abort when it returns False: before_type -> nothing typed; before_enter ->
# clear the composer and return failed_before_submit.
SendCheck = Callable[[CheckPoint], Awaitable[bool]]


class BrowserPort(Protocol):
    """What the coordinator may ask of the browser. Nothing else.

    Every method returns a typed status instead of raising for expected
    platform states. Navigations performed are reported so the coordinator can
    enforce `navigation_budget`.
    """

    async def inbox(self) -> InboxResult: ...

    async def open_row(self, row: InboxRow) -> ReadResult: ...

    async def read_thread(self, thread_id: str, want: int = 0) -> ReadResult: ...

    async def send_one(self, thread_id: str, text: str, check: SendCheck) -> SendResult: ...


# --- decision side (P05 implements, P03 consumes) ----------------------

@dataclass(frozen=True)
class ContextEvent:
    """An event as given to a model role. `event_id` is the storage row id."""

    event_id: int
    sender: Sender
    kind: Kind
    text: str
    reply_to_text: str | None = None
    media_label: str | None = None       # structural label, never interpreted visual content


@dataclass(frozen=True)
class DecisionInput:
    conversation_id: int
    contact_status: str                     # storage contact_status value
    relationship: dict                      # owner-configured register; {} if none
    owner_profile: dict                     # owner-configured profile; {} if none
    current_batch: tuple[ContextEvent, ...]  # the new inbound events being assessed
    recent: tuple[ContextEvent, ...]         # earlier same-thread events, oldest first
    corrections: tuple[str, ...] = ()        # scoped rules (P06); empty before then
    deterministic_cues: frozenset[str] = frozenset()
    facts: tuple[dict, ...] = ()
    examples: tuple[dict, ...] = ()
    memory_revision: str = "0:0"


@dataclass(frozen=True)
class RiskAssessment:
    risk: Risk
    reason_code: str
    action_hint: ActionHint
    signals: frozenset[Signal] = frozenset()
    evidence_event_ids: tuple[int, ...] = ()
    schema_version: int = 1


@dataclass(frozen=True)
class ReplyDraft:
    action: ReplyAction
    reason_code: str
    messages: tuple[str, ...] = ()           # empty when action == "skip"
    evidence_event_ids: tuple[int, ...] = ()
    used_memory_ids: tuple[int, ...] = ()
    schema_version: int = 1


@dataclass(frozen=True)
class FactProposal:
    facts: tuple[dict, ...] = ()             # validated in P06; opaque before then
    schema_version: int = 1


DecisionFailure = Literal[
    "budget_exhausted", "provider_error", "timeout", "refusal",
    "truncated", "empty", "invalid_output", "not_configured",
]


@dataclass(frozen=True)
class RoleOutcome:
    """Either a validated value or a named failure. Never both."""

    role: Role
    value: RiskAssessment | ReplyDraft | FactProposal | None = None
    failure: DecisionFailure | None = None
    usage: dict = field(default_factory=dict)  # tokens/latency; missing = unknown, not zero
    versions: dict = field(default_factory=dict)


class DecisionPort(Protocol):
    async def assess(self, data: DecisionInput) -> RoleOutcome: ...

    async def draft(self, data: DecisionInput, assessment: RiskAssessment) -> RoleOutcome: ...

    async def extract(self, data: DecisionInput) -> RoleOutcome: ...

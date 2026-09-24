# Technical requirements and contracts

Normative target contracts. Names below are proposed APIs to implement, not existing callable commands. SQL details may vary if invariants remain and the implementation records why.

## Runtime and settings

Python 3.11+, installed Playwright/Chromium, SQLite via `sqlite3`, Click. Keep existing dependencies where useful; a Telegram HTTP client dependency needs a recorded reason if existing/standard tools suffice. Pin a reproducible tested dependency set in P08.

`SHADOW_DATA_DIR` is an absolute private writable directory, defaults outside the checkout using the OS user data location. The runtime must never silently fall back to repository data. `SHADOW_PROVIDER` and `SHADOW_MODEL` have no production default until chosen. Credentials stay in environment/private config. Timezone is explicit IANA (owner setup may choose Asia/Kolkata); persist UTC timestamps.

Initial defaults to validate during implementation: reply delay uniform 120-180 s after the latest inbound event; maximum coalescing wait 300 s from first unhandled event, followed by reread and a new bounded batch if conversation is still changing; 4 bubbles maximum per reply, 500 characters per bubble, 1,200 total, no minimum length. These are safety ceilings, not style targets. Initial per-account caps: 20 autonomous turns/hour, 100/day; per-thread 6/hour. Owner commands share transport/account caps but are separately identified. Caps are configurable in private setup and exercised at boundaries. Report cap deferrals, not fake sends. Owner-directed immediate sends do not inherit the casual reply delay, but still respect transport limits and verification.

Fresh autonomous work older than 30 minutes is re-evaluated against live history; never dump a stale backlog. Initial polling interval 15 s while healthy, backed off to 60 s on idle/error. Active hours are optional owner settings, not inherited hard-coded hours. Urgent alerts bypass quiet hours; normal sends do not.

## Data contracts

Use dataclasses plus explicit validation initially; no new validation framework required.

- `ConversationRef`: local account ID, exact platform thread ID, observed participant identifiers if available, display label, binding status/evidence. Display label is presentation, never a primary key.
- `ObservedEvent`: local event ID, thread ID, sender (`owner|contact|unknown`), kind (`text|media|reaction|system|unknown`), text, observed UTC time, platform timestamp/ID when exposed, reply target when observed, source (`import|dom|confirmed_send`), confidence/evidence, sequence position, origin (`owner_authored|shadow_generated|unknown`).
- `ReadResult`: `ok|empty_verified|login_required|challenge|dom_unknown|wrong_thread|ambiguous_identity`, events, observed thread/account binding, completeness flag, pane revision. Empty list alone is not a status.
- `Decision`: schema version; `action=reply|skip|hold`; reason code; `risk=none|money|commitment|distress|privacy|unsupported|uncertain`; `messages` string list; evidence event IDs; used memory IDs; optional memory candidates. Non-reply actions require empty messages. Unknown fields/actions or malformed JSON invalidate the whole result.
- `SendResult`: outbox item ID; `confirmed|failed_before_submit|uncertain`; observed thread ID; matched event/evidence reference; error code. Browser appearance confirms only UI observation, not remote delivery/read receipt.
- `GenerationResult`: validated decision plus provider/model/settings, prompt/profile/schema versions, token usage when supplied, latency, finish reason, provider request ID if safe. Never persist chain-of-thought.

Evidence IDs must exist in the supplied context and match that thread. A model's claimed confidence is advisory and cannot override invalid identity, unsupported content, or deterministic policy. Initial decision reason codes: `ordinary_reply`, `conversation_complete`, `reaction_only`, `money_request`, `commitment_request`, `distress_clear`, `distress_uncertain`, `private_information`, `unsupported_content`, `context_missing`, `identity_uncertain`, `automation_question`, `output_invalid`. Extend with schema version/tests, not arbitrary provider prose. Model-controlled reason codes do not replace separately computed browser/transport errors.

## Required persisted records

| Record | Key fields and constraints |
| --- | --- |
| account/runtime | account binding, schema version, active configuration digest, pause reason, lease/process lock metadata |
| conversations | unique `(account_id, thread_id)`, binding evidence, observed label, policy, last checked time |
| events | stable local ID, thread foreign key, source, origin, observed/platform times; unique platform ID per thread when present |
| conversation_work | thread unique, inbound revision, due time, first pending time, state, cancellation generation |
| outbox | unique `(decision_id, item_index)`, thread, intended text, revision/config digest, state, attempt/evidence timestamps |
| decisions | immutable validated decision, source revision, versions, status, safe diagnostics |
| facts/corrections | records in [memory-design.md](memory-design.md), scope, provenance, validity, supersession |
| telegram_updates | unique update ID, authenticated source, accepted command, result/outbox reference |
| alerts | dedupe key, severity, reason, pending/sent/acknowledged/resolved state, attempt count, next attempt |
| migrations/imports | schema/import version, source fingerprint, counts, quarantined rows, verification status |

Enable foreign keys, explicit transactions, busy timeout, integrity checks, and a single writer. WAL is appropriate for local storage; do not put the database on an untested network filesystem. Disk-full/write failure before submission prevents sends. Private migration backup must be verified before switching authoritative storage.

## Observation identity and import

Never deduplicate solely by `(sender,text)`: repeated "ha" can be separate real messages. Prefer actually observed stable message IDs. Otherwise use ordered sequence alignment over overlapping windows with sender/kind/text/timestamps/reply context. Distinguish a repeat at a new position from a previously seen event. Preserve ambiguity and hold if overlap cannot establish safe identity; do not assert an exactly-once guarantee without server identifiers.

Preview hashes may avoid redundant discovery but cannot mark a conversation handled or identify its recipient. Exact route equality uses parsed numeric thread IDs, not substring containment. Imported folders are unbound until explicitly mapped or unambiguously corroborated; no substring/fuzzy contact match may bring another person's history into a prompt.

Legacy import is read-only toward originals, idempotent by source file fingerprint and row index, with a private quarantine report for malformed rows and missing senders. It preserves original timestamps and marks unknown authorship rather than assigning uncertain generated text to the owner. It does not import old reply hashes as delivery truth. P01 runs parallel validation on private copies and retains originals until owner-approved cleanup.

## Outbox state machine

```text
pending -> attempting -> confirmed
                     -> failed_before_submit
                     -> uncertain
pending -> cancelled  (pause/stale revision/changed policy)
failed_before_submit -> pending  (bounded retry after fresh checks)
uncertain -> confirmed  (unique reconciliation evidence)
uncertain -> held       (unresolved; owner decision required)
```

Before input, persist `attempting`. A crash during/after this state is uncertain, including a timeout after Enter. Persisting `confirmed` and inserting the sent event occur in one transaction. A row count increase, incoming message, existing matching old text, or composer clearing alone is insufficient confirmation.

Confirmation needs the same exact account/thread, a new outbound bubble matching intended text and ordering after the prior anchor, and no visible pending/error indication. Where the DOM exposes a stable ID, retain it. Matching must survive Unicode/linebreak normalization without collapsing distinct messages. If UI evidence cannot uniquely distinguish two identical messages, hold. Reconciliation scans a bounded current window; absence from a virtualized window is not proof of non-submission.

A partial burst records only confirmed items. Stop the remaining items on failure, pause, fresh inbound, or manual activity. Do not resend confirmed items, automatically restart an uncertain item, or silently regenerate a whole burst. An owner-approved retry after unresolved uncertainty explicitly records possible duplication.

## Coordinator and concurrency

All browser navigation/read/send operations acquire one shared lock; Telegram only enqueues work. The database claim and revision check prevent two workers or duplicate updates creating two outboxes. Model work executes asynchronously or in a bounded worker thread, with bounded concurrency and timeouts; it cannot block Telegram pause processing.

Recheck pause/cancellation immediately before typing and immediately before Enter, plus between bubbles. A pause cannot recall a message already submitted. Account/thread drift cancels input; pre-existing nonempty composer indicates manual activity and must not be overwritten. Owner activity in the DOM pauses that thread even though manual overlap is not the expected workflow.

Pause state survives restart. A process crash does not automatically resume uncertain sends. For unchanged eligibility, a safe pre-submit failure may retry at most twice with backoff and fresh checks. Generation/network retries are at most two additional attempts, respect retry hints, and never include a send retry. Stale queued drafts are invalidated by model, policy, profile, or correction-version changes.

## Model integration

Keep a small provider function boundary accepting neutral role/content input and returning the contract above. Capabilities declare context budget, structured-output support, supported sampling controls, timeout behaviour, and usage availability. Never assume all models at one provider support the same parameters.

Native structured output when available; otherwise require exactly one JSON object and validate strictly. Do not salvage arbitrary prose into a sendable message, silently truncate unsafe arrays, or treat malformed response as an intentional skip. One bounded schema repair is permitted within the total retry budget, followed by hold on failure. Truncation/refusal/empty content are distinct failure states.

Generation is normally one call that promptly combines contextual assessment and a candidate reply, with the candidate held until its send due time. High-risk keyword cues prioritize assessment/alerts but do not alone decide whether a remark is figurative. Coalesce quick arrivals briefly (at most 3 seconds) before ordinary assessment, with no such debounce for urgent cues. Bound simultaneous provider work to two requests per account; newer revisions invalidate older responses. Track generation attempts/tokens independently of send caps so an incoming spam burst cannot create unlimited paid calls; default ordinary-assessment cap is 60/hour. When the budget is exhausted, hold pending ordinary work and notify once; deterministic urgent alerts remain available without model calls. Uncertain risk receives one additional contextual assessment at most within the total attempt budget, or goes straight to hold. No automatic fallback to an unqualified model.

Switching models preserves canonical facts/history/corrections. It invalidates queued drafts and prior qualification for the new configuration digest. Context-size reductions trim by documented priority, never silently drop the current inbound event or governing policy. If mandatory context cannot fit, hold. Missing usage numbers mean unknown cost, not zero.

Qualification binds the model, provider capabilities, generation settings, system/policy/schema versions and base style profile. Normal new conversation facts and validated low-risk scoped corrections advance a memory revision and invalidate affected drafts, but do not require a full owner-rating cycle every time. Correction validation and analogous/contrast checks are required; material global style/policy changes require renewed qualification. Every decision records both the qualified base digest and the actual memory/profile revision used, so evolving memory is auditable without freezing learning.

## Diagnostics and dry run

Logs contain event IDs, states, timing, version hashes, and error codes; raw chat text, API tokens, contact labels, or full prompts require explicit private diagnostic capture. Dry-run reads may cause platform read receipts, but must not type, send, view stories, mutate production facts/outbox/handled state, or create default profiles. Use a read-only database view or disposable snapshot; evidence goes to an explicitly private output path.

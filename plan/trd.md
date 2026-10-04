# Technical requirements and contracts

Normative target contracts. Names below are proposed APIs to implement, not existing callable commands. SQL details may vary if invariants remain and the implementation records why.

## Runtime and settings

Python 3.11+, installed Playwright/Chromium, SQLite via `sqlite3`, Click. Keep existing dependencies where useful; a Telegram HTTP client dependency needs a recorded reason if existing/standard tools suffice. Pin a reproducible tested dependency set in P08.

`SHADOW_DATA_DIR` is an absolute private writable directory, defaults outside the checkout using the OS user data location. The runtime must never silently fall back to repository data. Provider/model settings have no production default until chosen. Credentials stay in environment/private config. Timezone is explicit IANA (owner setup may choose Asia/Kolkata); persist UTC timestamps.

## Settings (single source of truth)

This table is the only place numeric defaults are defined. Other plan documents, prompts, tests and code refer to the setting **name**, never a copied number. Code loads them from one validated settings module; tests read the same module. Changing a value needs a test at the boundary and a [decisions.md](decisions.md) entry; values affecting qualification invalidate it (see Model integration).

| Setting | Initial default | Meaning |
| --- | --- | --- |
| `mode` | `observe` | `observe` (read, assess, alert; never sends), `assisted` (owner approves every draft), `autonomous` (qualified routine replies send without approval). Fresh installs start in `observe`. |
| `autonomy_scope` | `none` | `none`, owner-listed contacts, or `known` (all bound, non-excluded contacts). Unknown contacts are never in scope. |
| `active_hours` | required at setup | Owner's waking window in the configured timezone. Outside it: no ordinary sends, drafts wait, polling uses `quiet_poll_interval`. Urgent alerts are unaffected. |
| `reply_delay_momentum` | 15-90 s | Delay when the thread had an outbound message within `momentum_window`. |
| `momentum_window` | 10 min | Defines an active back-and-forth. |
| `reply_delay_cold` | owner latency profile, clipped 60 s-20 min; 2-8 min without profile | Delay for a thread that was quiet. Profile is measured from owner-authored imported timestamps by time of day and relationship; no profile means the fallback range. |
| `coalesce_extension_max` | 300 s | Further inbound messages push the due time, but never beyond first pending event + sampled delay + this value. Then reread and form a new bounded batch. |
| `quick_coalesce` | 3 s | Brief grouping before ordinary risk assessment; none for urgent cues. |
| `takeover_window` | 30 min | After an owner-authored message not produced by the outbox, Shadow stops drafting in that thread for this long (risk alerts continue). `/resume <contact>` ends it early. |
| `stale_reeval` | 30 min | Work older than this is reassessed against a fresh read, never replayed. |
| `draft_ttl` | 60 min | Assisted draft awaiting approval expires to `held` and appears in the digest. |
| `burst_max_items` / `bubble_max_chars` / `reply_max_chars` | 4 / 500 / 1,200 | Transport safety ceilings, not style targets. No minimum length. |
| `send_cap_account` | 20/hour, 100/day | Autonomous + approved turns. Owner commands are counted and identified separately but share the transport cap. |
| `send_cap_thread` | 6/hour | Per conversation. |
| `pane_check_interval` | 15 s | In-page re-read of the already open tab. No navigation. |
| `dom_read_timeout_ms` | 15,000 ms | Total deadline for cold DOM readiness or one requested-history collection. Timeout never proves an empty thread. |
| `dom_settle_ms` / `dom_sample_ms` | 150 / 75 ms | Rendered-content quiet period and in-page geometry sampling. The settled steady-state read adds no wait. |
| `history_max_steps` / `history_step_timeout_ms` | 24 / 2,000 ms | Sequential overlapping scroll step and per-step wait ceilings, also bounded by `dom_read_timeout_ms`. No progress, ambiguity or a visual edge never proves complete server history. |
| `align_tail_events` | 200 events | Stored alignment tail and requested depth for one bounded discovery recovery. Recovery never bypasses manual, identity, group, owner or pause holds; pre-send rereads do not scroll-recover. |
| `inbox_check_interval` | 60 s | In-page read of the rendered thread list; navigates only if the list is not rendered. |
| `quiet_poll_interval` | 5 min | Inbox check outside `active_hours` (urgent detection only). |
| `idle_backoff_max` | 5 min | Error/idle backoff ceiling within active hours. |
| `navigation_budget` | 60/hour, 600/day | Page navigations (not in-page reads). Exhaustion pauses discovery and alerts once. |
| `risk_call_budget` / `reply_call_budget` / `extraction_call_budget` | 120 / 60 / 60 per hour | Paid-call budgets per role. |
| `provider_concurrency` | 2 | Simultaneous provider requests per account. |
| `telegram_outage_grace` | 15 min | Routine autonomy continues this long without Telegram, then pauses. |
| `urgent_repeat_schedule` | +5, +15, +30, +60 min | Re-alert for unacknowledged urgent alerts, then stop repeating and keep it pending. |
| `secondary_alert_after` | 15 min | Optional owner-configured secondary Telegram chat gets a minimal urgent notice if still unacknowledged. |
| `digest_schedule` | end of `active_hours`, plus on demand | Info-tier items and the sent-message review. |
| `confusion_downgrade` | >10% of autonomous turns over 7 days, minimum 30 turns | Automatic switch from `autonomous` to `assisted` with an alert. |

Retention values are owned by [memory-design.md](memory-design.md); fact-extraction size limits live there too.

Report cap/budget deferrals, not fake sends. Owner-directed immediate sends skip the reply delay but respect transport caps, identity and verification.

## Presence and account footprint

Owner read receipts are permanently disabled in the account settings (owner decision). The setup checklist records this; if a read shows receipts are visible, pause and alert. Opening a thread still clears its unread state on the owner's other devices, so every thread Shadow opened but did not answer appears in the digest. Rows whose preview alone proves they need no reply (owner-last, reaction to owner's message, attachment-only preview) are recorded from the preview without opening them. That leaves them unread on the phone; they get a digest line and are never answered.

Activity status ("Active now") will look continuous while the tab is open; the setup checklist recommends turning it off and records the owner's choice.

Footprint rules, which bound the automation pattern but do not guarantee against enforcement:

- One persistent tab. Steady state uses in-page reads; no reload loops, no inbox navigation per cycle, no story viewing, likes or profile browsing.
- Every navigation counts against `navigation_budget`. Deeper history loading happens only when context is insufficient.
- One consistent host, browser profile and network location. A data-centre IP is a known login-challenge risk; the owner must accept it explicitly in setup (see [operations.md](operations.md)).
- Any challenge, checkpoint, unusual-login or rate-limit screen is a global pause plus urgent alert. Never automate the challenge.

## Owner takeover

An owner-authored outbound event not produced by Shadow's outbox (owner replied from the phone) cancels pending drafts and outbox items for that thread and sets `owner_active_until = event time + takeover_window`. Detection happens on the next read, so a draft already submitted cannot be recalled. Risk assessment and urgent alerts continue during takeover. Composer text typed by the owner in the automation tab is a hold, as before.

## Data contracts

Use dataclasses plus explicit validation initially; no new validation framework required.

- `ConversationRef`: local account ID, exact platform thread ID, observed participant identifiers if available, display label, binding status/evidence. Display label is presentation, never a primary key.
- `ObservedEvent`: local event ID, thread ID, sender (`owner|contact|unknown`), kind (`text|media|reaction|system|unknown`), text, observed UTC time, platform timestamp/ID when exposed, reply target when observed, source (`import|dom|confirmed_send`), confidence/evidence, sequence position, origin (`owner_authored|shadow_generated|unknown`).
- `ReadResult`: `ok|empty_verified|login_required|challenge|dom_unknown|wrong_thread|ambiguous_identity`, events, observed thread/account binding, completeness flag, pane revision. Empty list alone is not a status.
- `RiskAssessment`: schema version; `risk=none|money|commitment|distress|privacy|unsupported|uncertain`; `reason_code`; `action_hint=proceed|hold|urgent`; `signals` (subset of `confusion`, `automation_question`, `owner_needed`); evidence event IDs. Produced by the risk role for every new inbound batch. Deterministic cues are computed separately and may raise priority to `urgent` or `hold`, never lower it.
- `ReplyDraft`: schema version; `action=reply|skip`; reason code; `messages` string list; evidence event IDs; used memory IDs. Requested only after a `proceed` assessment. `skip` requires empty messages. Unknown fields/actions or malformed JSON invalidate the whole result.
- `FactProposal`: produced asynchronously by the extraction role after an assessed batch; never blocks or gates a reply. Limits and validation are in [memory-design.md](memory-design.md).
- `Decision`: the coordinator's immutable record combining the assessment, optional draft, deterministic checks, mode, and final action `reply|skip|hold`. Only code writes it; no model output becomes a Decision without validation.
- `ApprovalRequest` (assisted mode): decision ID, Telegram message ID, state `pending_approval|approved|edited|rejected|expired|superseded`, owner edit text if any. Approval never bypasses identity, pause or revalidation checks.
- `SendResult`: outbox item ID; `confirmed|failed_before_submit|uncertain`; observed thread ID; matched event/evidence reference; error code. Browser appearance confirms only UI observation, not remote delivery/read receipt.
- `GenerationResult`: per call, the validated output plus role, provider/model/settings, prompt/profile/schema versions, token usage when supplied, latency, finish reason, provider request ID if safe. Never persist chain-of-thought.

Evidence IDs must exist in the supplied context and match that thread. A model's claimed confidence is advisory and cannot override invalid identity, unsupported content, or deterministic policy. Initial reason codes: `ordinary_reply`, `conversation_complete`, `reaction_only`, `money_request`, `commitment_request`, `distress_clear`, `distress_uncertain`, `private_information`, `unsupported_content`, `context_missing`, `identity_uncertain`, `unknown_contact`, `automation_question`, `confusion_signal`, `output_invalid`. Extend with schema version/tests, not arbitrary provider prose. Model-controlled reason codes do not replace separately computed browser/transport/takeover states.

## Required persisted records

| Record | Key fields and constraints |
| --- | --- |
| account/runtime | account binding, schema version, mode, autonomy scope, active configuration digest, pause reason, lease/process lock metadata |
| conversations | unique `(account_id, thread_id)`, binding evidence, observed label, contact status (`known|unknown_pending|approved_unknown|excluded`), policy, `owner_active_until`, last checked time |
| events | stable local ID, thread foreign key, source, origin (`owner_authored|owner_approved|shadow_generated|unknown`), observed/platform times; unique platform ID per thread when present |
| conversation_work | thread unique, inbound revision, due time, first pending time, state, cancellation generation |
| outbox | unique `(decision_id, item_index)`, thread, intended text, authority (`autonomous|owner_approved|owner_command`), revision/config digest, state, attempt/evidence timestamps |
| decisions | immutable validated decision, source revision, versions, status, safe diagnostics |
| approval_requests | decision foreign key, Telegram message ID, state, owner edit, timestamps |
| facts/corrections | records in [memory-design.md](memory-design.md), scope, provenance, validity, supersession |
| telegram_updates | unique update ID, authenticated source, accepted command, result/outbox reference |
| alerts | dedupe key, tier (`urgent|action|info`), reason, pending/sent/acknowledged/resolved state, attempt count, next attempt, digest inclusion |
| usage_counters | per-role calls, navigations and sends per window, for budgets and `/status` |
| migrations/imports | schema/import version, source fingerprint, counts, quarantined rows, verification status |

Enable foreign keys, explicit transactions, busy timeout, integrity checks, and a single writer. WAL is appropriate for local storage; do not put the database on an untested network filesystem. Disk-full/write failure before submission prevents sends. Private migration backup must be verified before switching authoritative storage.

## Observation identity and import

Never deduplicate solely by `(sender,text)`: repeated "ha" can be separate real messages. Prefer actually observed stable message IDs. P00b records whether the live DOM exposes any stable message/thread/participant identifiers; P01 designs for exactly those findings and still supports their absence. Otherwise use ordered sequence alignment over overlapping windows with sender/kind/text/media label/quoted text (D44); mutable badges, edit flags and relative ages are not matching identity. Accept only one contiguous new-tail placement, including unique full stored-block containment; competing placements hold. Distinguish a repeat at a new position from a previously seen event. Preserve ambiguity and hold if overlap cannot establish safe identity; do not assert an exactly-once guarantee without server identifiers.

Preview hashes may avoid redundant discovery but cannot mark a conversation handled or identify its recipient. Exact route equality uses parsed numeric thread IDs, not substring containment. Imported folders are unbound until explicitly mapped or unambiguously corroborated; no substring/fuzzy contact match may bring another person's history into a prompt.

Legacy import is read-only toward originals, idempotent by source file fingerprint and row index, with a private quarantine report for malformed rows and missing senders. It preserves original timestamps and marks unknown authorship rather than assigning uncertain generated text to the owner. It does not import old reply hashes as delivery truth. Where imported timestamps are reliable, it derives the owner latency profile used by `reply_delay_cold`; unreliable timestamps leave the fallback range in effect. P01 runs parallel validation on private copies and retains originals until owner-approved cleanup.

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

A partial burst records only confirmed items. Stop the remaining items on failure, pause, fresh inbound, owner takeover, or manual activity. Do not resend confirmed items, automatically restart an uncertain item, or silently regenerate a whole burst. An owner-approved retry after unresolved uncertainty explicitly records possible duplication.

## Operating modes

- `observe`: read, assess risk, raise alerts, write the digest. Never creates outbox items except explicit owner commands.
- `assisted`: every reply draft goes to Telegram as an `ApprovalRequest` with Send / Edit / Skip. Send enqueues the draft (`authority=owner_approved`) for `max(approval time, due time)`; Edit sends the owner's text (`owner_authored`) and records the difference as a correction candidate, offered to the owner as "make this a rule?", never auto-applied; Skip holds the turn with an optional reason. Approval triggers full revalidation: if the conversation changed since the draft, the old draft is superseded and a new one is proposed. Expired requests (`draft_ttl`) become held digest items.
- `autonomous`: qualified routine drafts for contacts inside `autonomy_scope` send at their due time without approval; everything else behaves as `assisted`. `confusion_downgrade` returns the installation to `assisted` automatically.

Mode changes are owner-only (Telegram or local CLI), persisted, and invalidate queued autonomous drafts. Unknown contacts are held in every mode until the owner approves the contact (`approved_unknown`), which permits cautious drafts but never imported relationship facts.

## Coordinator and concurrency

All browser navigation/read/send operations acquire one shared lock; Telegram only enqueues work. The database claim and revision check prevent two workers or duplicate updates creating two outboxes. Model work executes asynchronously or in a bounded worker thread, with bounded concurrency and timeouts; it cannot block Telegram pause processing.

Due time is the latest inbound event plus a delay sampled once per pending batch (`reply_delay_momentum` or `reply_delay_cold`), extended by later inbound events up to `coalesce_extension_max`. A due time outside `active_hours` moves to the next window start plus a freshly sampled cold delay; such work is always reassessed (`stale_reeval`) before sending.

Recheck pause/cancellation, mode, `active_hours`, `owner_active_until` and send caps immediately before typing and immediately before Enter, plus between bubbles. A pause cannot recall a message already submitted. Account/thread drift cancels input; pre-existing nonempty composer indicates manual activity and must not be overwritten. Owner activity in the DOM pauses that thread even though manual overlap is not the expected workflow.

Pause state survives restart. A process crash does not automatically resume uncertain sends. For unchanged eligibility, a safe pre-submit failure may retry at most twice with backoff and fresh checks. Generation/network retries are at most two additional attempts, respect retry hints, and never include a send retry. Stale queued drafts are invalidated by model, policy, profile, or correction-version changes.

## Model integration

Keep a small provider function boundary accepting neutral role/content input and returning the contracts above. Capabilities declare context budget, structured-output support, supported sampling controls, timeout behaviour, and usage availability. Never assume all models at one provider support the same parameters.

Three roles, each a separate call with its own prompt, schema and budget:

1. **Risk** (`risk_call_budget`): runs promptly on each new inbound batch after `quick_coalesce`, with no debounce when deterministic cues fire. Small context: current batch, recent thread, relationship register, relevant corrections. Output: `RiskAssessment`. Its prompt is purely classificatory; it is never asked to be casual or funny.
2. **Reply** (`reply_call_budget`): only after `proceed`. Full `ContextBundle`. Output: `ReplyDraft`, held until its due time.
3. **Extraction** (`extraction_call_budget`): asynchronous, lowest priority, after assessment. Output: `FactProposal`. Its failure or budget exhaustion never delays a reply or alert.

Each role is configured with one qualified model; roles may share a model. There is still no automatic fallback. Deterministic cues (keyword/pattern lists, currency and payment markers, self-harm phrases in English and Hinglish) raise priority and can force `hold`/`urgent`, but cannot decide that a remark is harmless. When the risk result is `uncertain`, one further risk call with more context is allowed within budget; otherwise hold.

Native structured output when available; otherwise require exactly one JSON object and validate strictly. Do not salvage arbitrary prose into a sendable message, silently truncate unsafe arrays, or treat malformed response as an intentional skip. One bounded schema repair is permitted within the total retry budget, followed by hold on failure. Truncation/refusal/empty content are distinct failure states.

Bound simultaneous provider work with `provider_concurrency`; newer revisions invalidate older responses. Budgets are tracked independently of send caps, so an inbound spam burst cannot create unlimited paid calls. When the reply or risk budget is exhausted, hold pending ordinary work and notify once; deterministic urgent alerts remain available without model calls.

Switching models preserves canonical facts/history/corrections. It invalidates queued drafts and prior qualification for the new configuration digest. Context-size reductions trim by documented priority, never silently drop the current inbound event or governing policy. If mandatory context cannot fit, hold. Missing usage numbers mean unknown cost, not zero.

Qualification binds, per role, the model, provider capabilities, generation settings, system/policy/schema versions and base style profile, recorded as one configuration digest (a hash; no heavier machinery in v1). Normal new conversation facts and validated low-risk scoped corrections advance a memory revision and invalidate affected drafts, but do not require a full owner-rating cycle every time. Correction validation and analogous/contrast checks are required; material global style/policy changes require renewed qualification. Every decision records both the qualified base digest and the actual memory/profile revision used, so evolving memory is auditable without freezing learning.

## Live quality monitoring

The risk role reports `confusion` (contact replies such as "??", "what", "who is this", "wrong chat") and `automation_question` signals on the batch following an autonomous or approved send. Either signal holds that thread, alerts the owner at `action` tier and marks the triggering decision for review. Rolling 7-day metrics: autonomous turns, approval/edit/skip rates (assisted), corrections per 100 sends, confusion signals, hold rate, alert counts per tier. `/status` and the digest report them; `confusion_downgrade` switches to `assisted` automatically.

## Diagnostics and dry run

Logs contain event IDs, states, timing, version hashes, and error codes; raw chat text, API tokens, contact labels, or full prompts require explicit private diagnostic capture. Dry-run reads must not type, send, view stories, mutate production facts/outbox/handled state, or create default profiles. Opening a thread still clears its unread state on the owner's devices (read receipts are off for this owner). Use a read-only database view or disposable snapshot; evidence goes to an explicitly private output path. `observe` mode is the production counterpart: it writes normal state but never sends.

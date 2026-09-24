# Architecture

Target design; not a description of completed runtime. See [feature.md](feature.md) for current gaps.

## Single installation

One Python process owns one SQLite database and one dedicated Instagram browser tab. A browser lock serializes every operation that depends on which thread is open. Telegram reception and provider calls may run concurrently, but they never manipulate that tab directly. A process lock prevents a second writer for the same account/data directory.

```mermaid
flowchart TD
    IG[Instagram rendered DOM] --> B[Browser adapter]
    B --> C[Coordinator: verify and schedule]
    T[Authenticated Telegram owner] --> C
    C <--> DB[(SQLite: events, outbox, memory, alerts)]
    DB --> X[Scoped context builder]
    C --> X
    X --> G[Provider adapter: one selected model]
    G --> V[Schema validation and policy gate]
    V --> C
    C --> S[Outbox worker: recheck before each send]
    S --> B
    C --> N[Durable notification worker]
    N --> T
```

No untrusted Instagram content or model output can call Telegram commands, SQL, shell, navigation, or send methods. The coordinator interprets a small validated decision structure; ordinary message text is data.

## Reuse and changes

| Existing code | Keep | Change |
| --- | --- | --- |
| `shadow/main.py`, Click CLI | Entry points and local diagnostics | Add onboarding, memory/status/control commands; avoid raw private logs |
| `shadow/config.py` | Environment loading and explicit settings | External private data directory, typed validation, timezone, limits, no hard-coded identity or selected model |
| `shadow/browser.py` | Persistent CDP connection, exact-host intent, pane extraction, trusted scrolling | Dedicated tab/account binding, exact thread checks, ambiguity states, observation identity, per-item send evidence |
| `shadow/loop.py` | Single coordinator and simple async runtime | Replace preview-name replay guard with durable work and revision checks; remove story work; no blocking model call on event loop |
| `shadow/memory.py` | Legacy import knowledge and usable-text filtering | SQLite repository, stable identity, provenance, retention, correction records; retire fuzzy matching for runtime authority |
| `shadow/brain.py` | Prompt separation idea and provider integration knowledge | Strict output contract, neutral profile, context risk decision, capability-aware adapters, fail closed on malformed output |
| `build_voice.py` | Statistics and extraction as diagnostics | Owner-authored/approved examples only, deduplication, train/dev/test separation before fitting, no mandatory averages |
| `make_eval_set.py`, `eval_voice.py` | Frozen cases and comparative reports | Full prior context, causal cutoff, multilingual/humour/risk rubrics, no private data required in CI |
| `drive.py`, `probe.py` | Read-only parser diagnostics | Same production parser and identity path; protect raw captures; no hidden sends |
| `test_shadow.py` | Relevant regression intent | Synthetic fixtures; behavioural checks replace source-text assertions |
| Root baseline documents | Historical explanation | New `plan/` authority; audit old text before publication |

New modules are justified only where responsibility is independent: proposed `storage.py` for schema/migrations if memory.py becomes unclear, `telegram.py`, `providers.py`, and `policy.py`. Do not create a plugin framework, abstract repository tree, worker hierarchy, or generic platform adapter for a single implementation.

## Read-to-send flow

1. Inspect visible inbox plus previously scheduled known threads. Do not depend on unread status: opening a DM may mark it read before processing completes.
2. Resolve candidate row by explicit UI evidence, open it, and bind exact numeric thread ID plus participant evidence. Duplicate names or missing participant evidence require a hold.
3. Read typed events from the actual message pane. Persist new observations transactionally. Stable DOM identifiers are optional observed capabilities, never assumed APIs.
4. Coalesce incoming events into one pending revision. Retrieve scoped context promptly and ask the selected qualified model for a structured risk decision and possible reply together. Clear urgent cues create a priority hold/alert immediately while assessment completes; do not wait for casual pacing.
5. Validate schema, source references, capability limits, and policy. Save a permitted candidate/draft and due time in a transaction; the LLM does not write state. Additional assessment is bounded and justified only for uncertainty. A new inbound batch invalidates the candidate and triggers fresh assessment.
6. Wait until the configured due time without blocking other work. Only then may a validated candidate become send-eligible. Skips and holds do not create sendable outbox items.
7. Reopen and reread exact thread, compare revision and manual-activity markers, check pause/auth/DOM health and current model/profile versions. Changed context cancels the stale draft and schedules new work; unchanged context does not need a second generation call.
8. Persist the next item as attempting, perform one browser send attempt, and record observed confirmation or uncertainty. Repeat only for items known not attempted and still valid.
9. Append confirmed events and the corresponding cursor/outbox transition atomically. A crash between browser and database requires reconciliation, not a repeated Enter press.

## DOM performance strategy

Retain Playwright as the browser control layer. The current code already uses page-side extraction and direct CDP wheel events; transport replacement alone will not remove Instagram rendering, network, or scrolling delays.

Use a persistent tab, reuse the open thread, collect fields in one in-page read, and fetch deeper history only when local context is insufficient. A MutationObserver may signal changes in the currently rendered pane; it does not observe every off-screen thread. Keep periodic inbox reconciliation and reinstall observers after navigation.

Measure navigation, readiness, extraction, history expansion, model time, deliberate delay, typing, and confirmation separately. Warm in-page extraction target: p95 below 500 ms on the measured host; end-to-end reads can take seconds. Do not trade identity evidence for latency, use Instagram private endpoints, or promise millisecond message arrival.

## Storage and trust

SQLite is the source of truth; JSON exports and voice snapshots are versioned derived artifacts. Keep the database, profile, logs, browser session, archives, backups, private tests, and generated examples outside the repository by default. TLS protects provider/Telegram transit; those services necessarily receive selected content. This is not an end-to-end encrypted bot chat or a claim of local-only processing.

Telegram has full owner authority only after numeric user AND private chat ID checks. Display names and incoming Instagram claims cannot establish authority. CDP is loopback-only or behind an explicitly secured private tunnel; never expose its port publicly.

## Failure model

Browser/auth/DOM failures pause sending; provider outages postpone bounded work; database corruption stops the process before external writes. Telegram failure retains alerts locally and applies the escalation policy in [operations.md](operations.md). The architecture favours conservative holds over pretending an uncertain send or unreadable conversation succeeded.

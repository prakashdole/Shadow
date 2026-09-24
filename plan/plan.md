# Shadow implementation plan

Specification version: 2.0, 2026-09-24. Status: planned, not implemented.

## Read this first

The goal is a dependable, self-hosted Instagram conversation agent whose casual English/Hinglish, humour, and relationship awareness improve through history and occasional owner corrections. Reliability and memory are part of the product, not polish after a chatbot demo.

This specification supersedes conflicting future-work guidance in the older root documents. Current source remains evidence of what exists, not proof that these requirements are implemented. User work already present in the checkout must be preserved.

## Document ownership

| Document | Source of truth |
| --- | --- |
| [prd.md](prd.md) | Product scope, owner choices, acceptance requirements |
| [trd.md](trd.md) | Runtime contracts, state transitions, configuration |
| [architecture.md](architecture.md) | Components, dependencies, flow, reuse decisions |
| [memory-design.md](memory-design.md) | Conversation memory, correction learning, portable schemas |
| [testing.md](testing.md) | Named test suites, quality thresholds, release evidence |
| [privacy.md](privacy.md) | Private data boundary and safe publication procedure |
| [operations.md](operations.md) | Telegram controls, installation, runtime failure handling |
| [phases.md](phases.md) | Ordered implementation stages and completion gates |
| [prompt.md](prompt.md) | Copyable instructions for a smaller execution model |
| [decisions.md](decisions.md) | Accepted design decisions and subsequent reasoning |
| [memory.md](memory.md) | Sanitized implementation checkpoint, not personal memory |
| [rules.md](rules.md) | Non-negotiable implementation invariants |
| [feature.md](feature.md) | Current-to-target feature mapping |

## Execution procedure

1. Give an execution model `plan/prompt.md`, naming the next incomplete phase. It must read the universal instructions and that phase's referenced documents.
2. It inspects current source, dirty/staged work, and `plan/memory.md`; it does not trust an earlier model's completion report alone.
3. It implements only that phase, verifies its gates, and records decisions, evidence, remaining limits, and a local stage commit when safely possible.
4. Repeat with the next phase, or allow one execution to continue through phases while each independent gate passes. A small model's context reset uses the checkpoint rather than improvising requirements.
5. Run the final independent review prompt only after implementation evidence is assembled. Missing credentials, chosen model, owner ratings, or controlled-live authorization remain explicit pending gates, never fabricated passes.

Implementation can proceed without repeated design interviews. External-account actions, destructive history changes, public release, or unavailable credentials are real activation boundaries. Automated testing cannot substitute for owner judgement of humour or prove future Instagram DOM stability.

## Milestones and order

- P00: publication containment, portable fixtures, baseline evidence.
- P01: canonical local state, migrations, event identity, durable outbox.
- P02: correct DOM reading and recipient verification, browser fixture harness.
- P03: safe sending, reconciliation, scheduling, full coordinator integration.
- P04: authenticated Telegram controls and durable notifications.
- P05: versioned provider-neutral generation and contextual risk decisions.
- P06: relevant memory, correction learning, natural conversational style.
- P07: held-out evaluation, adversarial tests, provider qualification.
- P08: packaging, deployment, recovery, resource measurements.
- P09: controlled acceptance, independent review evidence, release preparation.

P01-P03 form one reliability milestone; autonomous sending stays disabled throughout it. P04-P06 form the adaptation milestone. P07-P09 establish release evidence. Partial milestones are useful checkpoints, not launch approval.

## Explicit defaults and unresolved activation inputs

- Developer-first installation; one process/owner/account; English Instagram UI initially.
- Persistent Chromium controlled by Playwright/CDP; measure before replacing any transport.
- SQLite through the standard library; no Redis, vector database, microservices, fine-tuning, or model router for v1.
- One selected model at a time; selection is pending the owner, not hard-coded by this plan.
- Normally reply 120-180 seconds after the latest inbound event; timing does not imply an availability guarantee.
- Unknown contacts: only incoming one-to-one DMs, cautious style, no proactive outreach. Ambiguous identity stays paused.
- First release handles text. Unsupported media and groups produce a deduplicated owner notification, not invented interpretation.
- Source and synthetic fixtures are public; archives, facts, voice data, sessions, and private evidence are not.
- Hosting budget target is roughly USD 5/month excluding model API charges. Browser memory requirements must be measured before claiming it fits.
- Whether the repository was previously public is unconfirmed. Publication preparation must handle this as unknown exposure, not assume no exposure.
- Direct questions about automation must not receive a false denial. Routine replies use the owner's voice without assistant boilerplate. This is an explicit limitation relative to the requested unconditional denial rule.

## What this planning stage delivers

These documents and additive ignore protection only. It does not migrate data, remove files from Git, rewrite history, choose a model, send messages, or certify production readiness. Older runtime flaws are scheduled explicitly rather than silently corrected during planning.

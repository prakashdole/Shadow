# Shadow implementation plan

Specification version: 2.1, 2026-09-27 (post-review amendments D18-D28). Status: planned, not implemented.

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

- P00: publication containment, portable fixtures, baseline evidence, requirement matrix.
- P00b: read-only live DOM discovery (identifiers, preview signals, receipts/challenge indicators).
- P01: canonical local state, settings module, migrations, event identity, durable outbox.
- P02: correct DOM reading and recipient verification, browser fixture harness, in-page steady state.
- P03: safe sending, reconciliation, adaptive scheduling, active hours, takeover, modes, coordinator integration.
- P04: authenticated Telegram controls, assisted approvals, tiered alerts and digest.
- P05: provider-neutral risk/reply/extraction roles, contextual policy, live quality guard.
- P06: relevant memory, correction learning, natural conversational style.
- P07: held-out evaluation, adversarial tests, provider qualification.
- P08: packaging, home-host deployment, hardening, recovery, resource measurements.
- P09: controlled acceptance, assisted pilot, release preparation.

P00b-P03 form the reliability milestone and run only in `observe`. P04-P05 complete the **assisted milestone**: the first stage the owner uses day to day, drafting replies for Send/Edit/Skip in Telegram once a model is qualified for assisted use. P06-P07 improve memory and establish quality evidence. P08-P09 deliver deployment, the assisted pilot and, after it passes, owner-scoped autonomy. Partial milestones are useful checkpoints, not launch approval. v1 scope and deferred items are listed in [prd.md](prd.md#v1-core-and-deferred-scope).

## Explicit defaults and unresolved activation inputs

- Developer-first installation; one process/owner/account; English Instagram UI initially.
- Persistent Chromium in a dedicated profile controlled by Playwright/CDP; measure before replacing any transport.
- SQLite through the standard library; no Redis, vector database, microservices, fine-tuning, or model router for v1.
- One selected model per role (risk, reply, extraction; may be the same); selection is pending the owner, not hard-coded by this plan.
- Fresh installs start in `observe`; the owner moves to `assisted`, then to `autonomous` for chosen contacts.
- Pacing adapts to conversation momentum and the owner's measured reply latency within owner-set active hours; all numbers live in the [TRD settings table](trd.md#settings-single-source-of-truth). Timing does not imply an availability guarantee.
- Owner read receipts are permanently off. Activity status is an owner choice made at setup.
- Unknown contacts: held until the owner approves them, then cautious drafts only; no proactive outreach. Ambiguous identity stays paused.
- First release handles text. Unsupported media and groups go to the digest, not invented interpretation.
- Source and synthetic fixtures are public; archives, facts, voice data, sessions, and private evidence are not.
- Recommended host is an always-on machine on the owner's home network; a VPS is supported but raises login-challenge risk. Hosting budget target is roughly USD 5/month excluding model API charges. Browser memory requirements must be measured before claiming it fits.
- The original GitHub repository was observed public on 2026-09-27 with owner-identifying prompt text; see [privacy.md](privacy.md#current-release-blocker). Publication preparation treats it as already exposed.
- Direct questions about automation must not receive a false denial. Routine replies use the owner's voice without assistant boilerplate. This is an explicit limitation relative to the requested unconditional denial rule.

## What this planning stage delivers

These documents and additive ignore protection only. It does not migrate data, remove files from Git, rewrite history, choose a model, send messages, or certify production readiness. Older runtime flaws are scheduled explicitly rather than silently corrected during planning.

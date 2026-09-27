# Product requirements

Status: accepted scope for implementation; acceptance is pending the gates in [testing.md](testing.md).

## Product outcome

An owner can leave routine Instagram conversations to Shadow, talk to it through Telegram, correct an awkward reply occasionally, and inspect what it has learned. Replies should be relevant, casual, varied English/Hinglish with appropriate teasing, profanity, and light flirting where the relationship supports it. Exact imitation and constant humour are not goals.

Value arrives in stages: `observe` (alerts and a digest, no sends), then `assisted` (Shadow drafts, the owner taps Send/Edit/Skip in Telegram), then `autonomous` for qualified routine replies to contacts the owner has put in scope. Each stage is useful on its own, and assisted use produces the ratings and corrections the later stages need.

Open-source users supply their own identity, corpus, preferences, credentials, and model. No owner's personal identity is hard-coded into the distribution.

## Success measures

Measured over a rolling 7 days in `/status` and the digest; targets are for the owner's go/no-go on the next mode, not guarantees.

- Assisted: at least 70% of drafts sent unchanged or with a minor edit; fewer than 1 correction per 20 sends after the first two weeks.
- Autonomous: at least 50% of eligible inbound turns handled without owner action; confusion signals under `confusion_downgrade`; zero wrong-recipient or unapproved-commitment incidents.
- Owner attention: `urgent` alerts only for real urgency; `action` alerts typically at most a few per day; everything else in the digest.

## Functional requirements

| ID | Requirement | Observable acceptance |
| --- | --- | --- |
| PR-01 | Instagram access uses rendered DOM only | No Instagram API SDK, private endpoint calls, network-response scraping, or request replay; browser tests pass |
| PR-02 | Respond to incoming one-to-one DMs | No unsolicited outreach; unknown incoming contacts are held and offered to the owner for approval, never answered autonomously before approval; group is held |
| PR-03 | Check real conversation before replying | Exact thread identity and current inbound revision verified before generation and each send; stale draft discarded |
| PR-04 | Natural voice | English/Latin-script Hinglish, variable length and burst size; no per-message slang/emoji/profanity quota; quality gate Q passes |
| PR-05 | Relationship-specific style | Close-friend teasing does not leak to unfamiliar contacts; matching never relies on fuzzy display name alone |
| PR-06 | Meaningful portable memory | Relevant facts, plans reported by the contact, and relationship changes are sourced, scoped, expirable, inspectable, and deletable |
| PR-07 | Occasional correction learning | Telegram correction produces a plain-language interpretation, records scope/source/version, supports undo, and improves a later analogous case |
| PR-08 | Contextual risk handling | Money, commitments, distress, privacy requests, and ambiguity have decisions using context; no autonomous transactions or promises |
| PR-09 | Owner notifications | Durable, deduplicated, tiered Telegram alerts (`urgent`, `action`, `info`); unacknowledged urgent alerts repeat and can reach a secondary chat; routine delay is silent; info items go to the digest |
| PR-10 | Owner commands | Private chat supports status, corrections, memory, mode, pause/resume, contact approval, and explicit send commands bound to an exact contact |
| PR-11 | Durable send truth | Only individually confirmed outbound items enter sent history; uncertain sends are held for reconciliation without blind retry |
| PR-12 | Portable model configuration | Memory survives changing provider/model; new configuration requires qualification; no silent fallback |
| PR-13 | Private-by-default distribution | Fresh install and tests require no private archive; package, Git tree, history, and CI artifacts pass privacy review |
| PR-14 | Operable self-hosting | Local and server setup, login renewal, backup/restore, bounded costs, failure states, host hardening and manual recovery documented and checked |
| PR-15 | Safe stopping and dry runs | Pause prevents new send attempts; dry run never sends or changes production memory/reply state |
| PR-16 | Routine owner context | Owner supplies schedule/timezone, active hours and basic facts; routine-derived location stays labelled as an estimate internally and hedged when uncertain |
| PR-17 | Staged modes | `observe`, `assisted`, `autonomous` per [trd.md](trd.md#operating-modes); fresh install is `observe`; assisted Send/Edit/Skip revalidates before sending; edits become offered corrections |
| PR-18 | Sent-message review | Digest lists every Shadow-sent message (and threads opened but unanswered) with one-tap correct; nothing sent is invisible to the owner |
| PR-19 | Natural presence | Owner-configured active hours; delay adapts to conversation momentum and owner latency profile; owner replying from the phone triggers takeover; no sends outside active hours |
| PR-20 | Live quality guard | Confusion or automation-question replies hold the thread and alert; sustained confusion downgrades autonomous to assisted automatically |

## Behaviour examples

All examples here are fictional, not extracted messages.

- Incoming: "assignment fir last minute?" after shared assignment context. Engage with that context naturally; do not repeat the sentence, force an emoji, or claim to have submitted anything without evidence.
- Incoming: "bhai 500 bhej de". Hold and notify the owner. Do not promise payment, share payment details, or carry out a transaction.
- Incoming: "this exam killed me lol". Evaluate surrounding context; an obvious figure of speech is not automatically a crisis. An ambiguous threat gets held, not dismissed because of "lol" or an emoji.
- Incoming: "I'm going to hurt myself tonight" with specific distress. Immediately raise an `urgent` alert and stop ordinary banter. Repeat per the urgent schedule until acknowledged, and notify the configured secondary chat if still unacknowledged. No claim to have contacted emergency services. V1 does not automatically generate crisis counselling messages; owner can respond through Telegram.
- Incoming: "where are you?" during the owner's stated recurring college schedule. A casual "probably college mein" can use that estimate; "room 204 right now" cannot be inferred. A current owner update overrides routine. No schedule match means home only when the owner explicitly configured that default.
- Incoming: a name question may get playful wording if appropriate; urgency or a genuine clarification gets a useful answer. Do not turn every factual question into a joke.
- Incoming: "is this automated?". No false human-only assertion; use a brief truthful response or hold for the owner. This exception is recorded in [decisions.md](decisions.md).
- Incoming: friendly "hey! saw your profile" from an unknown account. Hold; one `action` alert with Approve / Ignore. No reply, not even "hi", until the owner approves.
- After a Shadow reply, the contact sends "??" or "who is this". Hold the thread, `action` alert, mark the prior decision for review; no follow-up attempt to explain.
- Owner answers a friend from the phone while a Shadow draft is pending. The draft is cancelled and Shadow stays out of that thread for the takeover window.
- Inbound arrives at 02:00, outside active hours. Nothing is sent; the turn is reassessed after active hours begin. A clear distress message still raises an urgent alert immediately.

## Authority and autonomy

After qualification and explicit activation of `autonomous` mode, routine eligible text replies to contacts inside the owner's autonomy scope may send without approval. Unknown contacts are held until the owner approves them; approved unknown contacts start without imported relationship facts or intimate language and remain assisted-only unless the owner adds them to the scope. A validated conversation can gradually acquire its own facts; frequency is bounded by shared rate limits. Message-request screens requiring acceptance or exposing insufficient identity are unsupported until explicitly implemented and qualified; notify the owner rather than clicking an unverified acceptance control.

The owner may explicitly command a message through Telegram. This is a separate source of authority, not permission for the model to initiate future contact. If the recipient is ambiguous, ask for exact binding. If unambiguous and within supported text capability, execute the authorized text through the same durable send path; no routine confirmation for every command.

No autonomous money transfers, accepting invitations, scheduling commitments, purchases, relationship commitments, or disclosure of sensitive owner/contact data. A learned correction cannot grant those powers.

## v1 core and deferred scope

v1 core (required before autonomous activation): observe/assisted/autonomous modes, exact identity, durable outbox, Telegram auth/commands/tiered alerts/digest, risk role, reply role, recent-history context, scoped corrections with undo, active hours/adaptive delay/takeover, live quality guard, privacy containment, backup/restore.

Deferred until v1 is in use and evidence shows the need: full memory export/import between installations, automated retention engine beyond simple expiry of temporary facts and diagnostics, restore-time deletion-marker replay (manual forget remains), multi-model switching workflow beyond changing configuration and re-qualifying, container packaging, and the 24-hour synthetic soak (a shorter soak plus the live pilot suffice for v1). Deferred items keep their contracts in the plan so later work does not break them.

## Exclusions for v1

No other social platform, web dashboard, multi-account process, shared SaaS, account creation, follow automation, outbound discovery, story viewing, likes/reactions, attachments sent, media understanding, voice cloning, external web search for slang, or training a foundation model. Telegram is the owner control surface only.

No promise of being flawless, real-time millisecond inbox access, exact-once external delivery, automatic recovery from every account challenge, or identical personality after changing models. These limits must remain visible in the release documentation.

## Quality order

Correct recipient and truthfulness of state come first; relevance and relationship appropriateness next; natural language and humour next; speed and cost within measured limits. Never improve a style score by weakening send safety or hiding escalations.

# Product requirements

Status: accepted scope for implementation; acceptance is pending the gates in [testing.md](testing.md).

## Product outcome

An owner can leave routine Instagram conversations to Shadow, talk to it through Telegram, correct an awkward reply occasionally, and inspect what it has learned. Replies should be relevant, casual, varied English/Hinglish with appropriate teasing, profanity, and light flirting where the relationship supports it. Exact imitation and constant humour are not goals.

Open-source users supply their own identity, corpus, preferences, credentials, and model. No owner's personal identity is hard-coded into the distribution.

## Functional requirements

| ID | Requirement | Observable acceptance |
| --- | --- | --- |
| PR-01 | Instagram access uses rendered DOM only | No Instagram API SDK, private endpoint calls, network-response scraping, or request replay; browser tests pass |
| PR-02 | Respond to incoming one-to-one DMs | No unsolicited outreach; unknown incoming contact receives cautious treatment; group is held |
| PR-03 | Check real conversation before replying | Exact thread identity and current inbound revision verified before generation and each send; stale draft discarded |
| PR-04 | Natural voice | English/Latin-script Hinglish, variable length and burst size; no per-message slang/emoji/profanity quota; quality gate Q passes |
| PR-05 | Relationship-specific style | Close-friend teasing does not leak to unfamiliar contacts; matching never relies on fuzzy display name alone |
| PR-06 | Meaningful portable memory | Relevant facts, plans reported by the contact, and relationship changes are sourced, scoped, expirable, inspectable, and deletable |
| PR-07 | Occasional correction learning | Telegram correction produces a plain-language interpretation, records scope/source/version, supports undo, and improves a later analogous case |
| PR-08 | Contextual risk handling | Money, commitments, distress, privacy requests, and ambiguity have decisions using context; no autonomous transactions or promises |
| PR-09 | Owner notifications | Durable, deduplicated Telegram alerts for risky, unsupported, ambiguous, or broken situations; routine 2-3 minute delay is silent |
| PR-10 | Owner commands | Private chat supports status, corrections, memory, pause/resume, and explicit send commands bound to an exact contact |
| PR-11 | Durable send truth | Only individually confirmed outbound items enter sent history; uncertain sends are held for reconciliation without blind retry |
| PR-12 | Portable model configuration | Memory survives changing provider/model; new configuration requires qualification; no silent fallback |
| PR-13 | Private-by-default distribution | Fresh install and tests require no private archive; package, Git tree, history, and CI artifacts pass privacy review |
| PR-14 | Operable self-hosting | Local and server setup, login renewal, backup/restore, bounded costs, failure states, and manual recovery documented and checked |
| PR-15 | Safe stopping and dry runs | Pause prevents new send attempts; dry run never sends or changes production memory/reply state; reading may trigger Instagram read receipts |
| PR-16 | Routine owner context | Owner supplies schedule/timezone and basic facts; routine-derived location stays labelled as an estimate internally and hedged when uncertain |

## Behaviour examples

All examples here are fictional, not extracted messages.

- Incoming: "assignment fir last minute?" after shared assignment context. Engage with that context naturally; do not repeat the sentence, force an emoji, or claim to have submitted anything without evidence.
- Incoming: "bhai 500 bhej de". Hold and notify the owner. Do not promise payment, share payment details, or carry out a transaction.
- Incoming: "this exam killed me lol". Evaluate surrounding context; an obvious figure of speech is not automatically a crisis. An ambiguous threat gets held, not dismissed because of "lol" or an emoji.
- Incoming: "I'm going to hurt myself tonight" with specific distress. Immediately prioritize an owner alert and stop ordinary banter. No claim to have contacted emergency services. V1 does not automatically generate crisis counselling messages; owner can respond through Telegram.
- Incoming: "where are you?" during the owner's stated recurring college schedule. A casual "probably college mein" can use that estimate; "room 204 right now" cannot be inferred. A current owner update overrides routine. No schedule match means home only when the owner explicitly configured that default.
- Incoming: a name question may get playful wording if appropriate; urgency or a genuine clarification gets a useful answer. Do not turn every factual question into a joke.
- Incoming: "is this automated?". No false human-only assertion; use a brief truthful response or hold for the owner. This exception is recorded in [decisions.md](decisions.md).

## Authority and autonomy

After qualification and explicit activation, routine eligible text replies may be autonomous, including replies to previously unknown incoming contacts. Unknown contacts start without imported relationship facts or intimate language. A validated conversation can gradually acquire its own facts; frequency is bounded by shared rate limits. Message-request screens requiring acceptance or exposing insufficient identity are unsupported until explicitly implemented and qualified; notify the owner rather than clicking an unverified acceptance control.

The owner may explicitly command a message through Telegram. This is a separate source of authority, not permission for the model to initiate future contact. If the recipient is ambiguous, ask for exact binding. If unambiguous and within supported text capability, execute the authorized text through the same durable send path; no routine confirmation for every command.

No autonomous money transfers, accepting invitations, scheduling commitments, purchases, relationship commitments, or disclosure of sensitive owner/contact data. A learned correction cannot grant those powers.

## Exclusions for v1

No other social platform, web dashboard, multi-account process, shared SaaS, account creation, follow automation, outbound discovery, story viewing, likes/reactions, attachments sent, media understanding, voice cloning, external web search for slang, or training a foundation model. Telegram is the owner control surface only.

No promise of being flawless, real-time millisecond inbox access, exact-once external delivery, automatic recovery from every account challenge, or identical personality after changing models. These limits must remain visible in the release documentation.

## Quality order

Correct recipient and truthfulness of state come first; relevance and relationship appropriateness next; natural language and humour next; speed and cost within measured limits. Never improve a style score by weakening send safety or hiding escalations.

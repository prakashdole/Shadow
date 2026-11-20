# Shadow product requirements

## Product outcome

A friend sends a reel followed by “this is literally us.” Shadow should understand which reel the friend means, read the accompanying message and draft one reply that fits the relationship. The owner should be able to tell when the reply used the caption, visible action or audio, and when the evidence was too thin to use.

The first deliverable is smaller than a working bot. Given a saved screenshot and a short list of permitted actions, Shadow identifies the next step or waits. The first integrated release ends at a draft. Instagram sending comes later.

## Conversations in scope

| Situation | Expected experience |
| --- | --- |
| A known contact sends plain text | Shadow reads the recent exchange in order, distinguishes the two speakers and drafts a relevant reply. |
| A known contact shares a reel | Shadow associates the card with its message, inspects the accessible media and drafts from the evidence it collected. |
| A caption or follow-up changes the meaning | The reply model sees the surrounding conversation and the reel together. A question about the soundtrack requires audio or a reliable transcript. |
| Several cards are visible | Shadow opens only the card it can associate with the intended turn. If that association is unclear, the draft waits. |
| A reel is removed, stalls or requires another login | Shadow records the reason. It can still draft from the accompanying text when that text makes sense on its own. |
| A new message arrives or the owner answers on their phone | Shadow discards the stale draft. An owner reply yields control of the conversation to the owner. |
| An unfamiliar contact, group, ambiguous recipient or sensitive commitment appears | Shadow holds the conversation for owner review. A model’s confidence cannot authorize a new contact, payment or promise. |

## Voice, memory and owner control

Preserve the original product’s relationship-specific English and Hinglish, variable reply length and occasional humour. Use recent turns, relevant facts and approved examples for this contact. Quoted text belongs to the quoted speaker. A phrase in a reel is media content, never a fact about the contact without additional evidence.

The owner can inspect a draft, correct it, skip it or pause processing. The longer-term assisted workflow uses the existing private Telegram controls and binds approval to a recipient and a particular draft. Owner corrections carry scope and provenance and can be undone. Later autonomy is opt-in for individual contacts and respects active hours, manual takeover and urgent escalation.

## Acceptance

- **Correct action.** On held-out labeled screenshots, report correct choices, unnecessary holds and incorrect taps separately. A wrong-thread or wrong-card selection blocks that action from the browser trial until the cause is understood and retested.
- **Faithful reading.** Compare extracted messages with a human transcript, including order, authorship, quoted replies and mixed-script text. Report uncertainty where the screenshot cuts off a bubble. Visible history is always a bounded observation.
- **Grounded reel replies.** The owner judges whether the draft addresses the sender’s point. Count invented dialogue, mistaken reel references and replies that depend on missed audio as failures.
- **Useful drafts.** Record accept, minor edit, rewrite and skip with a reason. The prior assisted target of 70% accepted unchanged or with minor edits remains a later pilot target. A navigation benchmark alone cannot qualify reply quality.
- **Privacy.** Use fictional or redacted examples for development. Keep private capture, session and voice material outside published fixtures. The owner should know which cropped content reaches each provider.

The initial scope is incoming one-to-one chats on one account with an English UI. Full-history export, background outreach and interaction with likes, follows or stories are outside this workflow.

# Shadow technical requirements

These contracts specify the proposed screenshot path. Field names below illustrate the information each boundary must carry. Implementations should first examine the existing Shadow types and reuse compatible ones.

## Capture, decision and execution

| Boundary | Required information | Validation |
| --- | --- | --- |
| Captured screen | Capture ID, session and tab, time, screen dimensions, viewport, scale, raw image and any crop or resize transform | Reject blank or partial captures. A changed viewport invalidates the calibration. Keep decision labels off the original OCR image. |
| Candidate target | Capture ID, action key, visible label or anchor, bounding box in source pixels, expected next screen | Every target must fit the current viewport. A moving row or card needs fresh positional evidence. Omit targets obscured by an overlay. |
| Decision request | Required screenshot or crop from the current screen, plus the task and descriptions of available choices, including `wait` or `hold` | Wait for a fresh image if capture fails. Keep OCR output and copied chat text out of the request. Supply only actions legal in this state. Treat chat and reel content visible in the image as untrusted. Crop unrelated private conversations when possible. |
| Decision response | Winning choice, probability/confidence, request and capture association | Validate the returned key and bind it to the pending request. A low-confidence, ambiguous or contradictory result holds. Set cutoffs from held-out examples for each action. |
| Execution result | Chosen action, pre-action evidence, resulting screen class and reason for success or hold | Recheck that the target is still visible and the active thread is unchanged. Tap once, then wait within a deadline for the expected visual change. On timeout, capture fresh evidence before deciding again. |

Use the capture-to-input transform exactly once. In a calibrated test, clicking the center of a labeled rectangle must hit the same control at the chosen device scale. Include a resized model image and a cropped input in this check. A valid choice applied in the wrong coordinate system is still a failed action.

For a prototype screenshot showing two labeled cards and the sender’s latest bubble, offer “open card 1,” “open card 2,” “read the text,” “wait for rendering,” and “hold because the referent is unclear.” Each option describes the visible evidence that warrants it. A second independent question can classify the turn as text, reel, mixed or unclear. Resolve inconsistent answers locally. Decision 1 selects from supplied options and returns probabilities. Free-form transcription and reply generation need separate components. [Decisions API examples](https://codelabs.developers.google.com/getting-started-with-gemini-decisions-api)

## Screen readiness and message actions

Classify the latest screenshot before offering an action. A visible chat header with a spinner, an unrendered message pane or a missing composer is still loading. Wait and recapture within the existing deadline. A reel overlay can leave the DM visible underneath it, so the overlay must be closed and the intended chat rechecked before drafting. Use Decision 1's screen classification as evidence and require the target and its expected screen state to agree before clicking.

- **Quote a message or reel.** Bind the Reply arrow to the specific visible bubble or card. Verify the quote preview in the DM composer before inserting a draft. The quote preview and unsent text can be exercised in draft mode.
- **React to a message or reel.** Bind the React control to the specific bubble or card. Choose an approved emoji from the visible picker and verify the reaction on that same item. Selecting the emoji publishes the reaction, so offer that action only in an enabled interaction mode. Exercise the picker and emoji choice on fixtures during draft-only testing.

## Conversation identity and evidence

Bind the session to the expected account and confirm the visible conversation header after opening a row. A local contact record needs distinguishing visible evidence, such as a verified handle where available. Duplicate display names, uncertain sender direction or a missing header require a hold. A screenshot hash identifies one image only. Repeated message text cannot uniquely identify a person or event.

The reader returns an ordered window of visible bubbles. Keep the text, sender evidence, quote relationship, media-card reference, partial-visibility status and capture ID together. When scrolling, align overlapping windows only where the match is unambiguous. Preserve adjacent identical messages. A disappeared bubble can result from scrolling or reflow. Mark observation coverage as partial and avoid inferring deletion, edits or exact timestamps from absence.

## Routing text and reels

1. **Text.** Send the original conversation crop to `gemini-3.5-flash-lite`. Keep visual order, sender side, verbatim text, quote and media links, and mark unreadable or clipped portions. Preserve repeated bubbles. Do not pass that transcript into the Decision 1 request. Hold if a name, price, time or sender the draft needs is unreadable.
2. **Reel or mixed turn.** Bind the card to the incoming message before opening it. Retain the caption, nearby text, and a private local reference linking the evidence to this draft. A local reference does not imply a platform permalink. Verify the player opened and playback advanced before treating samples as video.
3. **Sampling experiment.** Include the first visible frame. Trial 1-, 2- and 4-second intervals on the same clips, retaining timestamps. Start with a proposed cap of 12 distinct frames per draft for the comparison. Record the elapsed interval covered and whether the reel ended, looped, stalled or exceeded the budget. A long reel may have an unseen ending. Empty, repeatedly identical or buffering frames require a partial or unavailable result.
4. **Audio.** Stills omit spoken dialogue and soundtrack. Compare timestamped frames with a short audio-bearing clip or a verified transcript on examples whose meaning depends on speech. Mark audio availability explicitly. Test browser audio capture independently from playback. [Gemini video input guidance](https://ai.google.dev/gemini-api/docs/video-understanding)
5. **Reel interpretation.** Send the captured frames or clip, caption and available audio or transcript to the smaller multimodal model. Ask for a short description of the subject, what happens, and relevant on-screen text or speech. Record which evidence was available and which details remain unclear. Attribute speech only when supported by audio, captions or a verified transcript.
6. **Reply.** Give the main model the ordered recent conversation, scoped context and the associated reel summary in one request. Ground statements about dialogue and action in the available evidence. If the sender’s question depends on missing media, return a hold. After leaving the player, recheck the thread before accepting further UI actions.

## Drafts, sending and recovery

Keep the draft, contact, input revision, evidence coverage and owner decision together. The screenshot pilot ends at a saved draft. Later sending requires a separately qualified route through the existing approvals and outbox logic. At execution time, recheck account, recipient, latest inbound turn, mode, pause and owner takeover. An edit or a newer turn expires the old approval.

Persist the attempt before the external action. Record delivery only with evidence of a new outgoing bubble in the intended thread that can be attributed to that attempt. If the browser or process stops between the action and confirmation, mark the outcome uncertain and reconcile before further attempts. A matching older bubble or an empty composer cannot confirm delivery.

## Operational requirements

- Bound the number of actions, captures, model calls and retries per turn. Tune limits using time, memory, transferred bytes and provider usage observed on the target host. A timeout returns a reason and keeps the pending state recoverable.
- Pause on authentication challenges, unexpected navigation or account changes. A provider outage leaves work pending. Pause and owner takeover must remain responsive during an in-flight request.
- Keep browser session data, provider keys, raw captures and optional recordings private. Prefer temporary in-memory crops and retain diagnostics only deliberately. The default log contains timings, action names, outcomes and counts. Never log raw DMs, images, auth headers or API keys.
- Record browser build, viewport, model ID, sampling settings and input resolution with evaluation results. Keep offline fixture scores, real API responses, browser compatibility and live-account outcomes distinguishable. Older DOM results cannot qualify screenshot actions.

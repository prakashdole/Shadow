# Shadow implementation plan

The **first milestone** is to prove that Gemini Decision 1 can choose the intended action from a saved screenshot, including when it should wait. Browser choice follows that result. The screenshot path, reel pipeline and new test suite are planned work. The repository currently contains the Python CLI, DOM observer, memory, storage and owner-control code from earlier revisions.

## 1. Test the Decisions API in isolation

Prepare two separate sets of fictional or redacted screenshots. Label the visible screen, intended action, valid target and reason to hold before writing the prompt. Include a plain DM, text plus a reel, two similar cards, a reordered inbox, a DM header with its pane still loading, a loading player, a reel overlay above the DM, a quote preview, a reaction picker and an unfamiliar layout. Keep near-identical captures from one conversation in the same split.

Use hand-labeled regions so a failure in screen detection cannot mask a failure in the API. Make a real request to `gemini-decision-1` with the screenshot as the only evidence about the screen and the named choices as instructions. Leave OCR text and copied messages out of the test. Record the winner, confidence, latency, failures and billable usage where available. Tune the descriptions and hold threshold on the development set, then freeze them before scoring the held-out set.

**To pass**, produce a replayable fixture set, a real response for a known case and a held-out table by screen type. Count false taps and unnecessary holds separately. A confident wrong-recipient or wrong-card result blocks that action. If the API is unavailable, record the provider error and leave this gate open. A successful mock verifies only the harness.

## 2. Qualify Obscura and the target map

Use a render-enabled Obscura build at a fixed viewport. Start on a local page that reports which control actually received the click. Exercise fixed hotspots, numbered moving rows, scrolling, text entry, an overlay and a layout change between capture and click. Run this part with automatic region detection. Hand-labeled regions from phase 1 would hide runtime targeting errors.

Then exercise a read-only Instagram session with an appropriate test account. Check authentication survives restart, DM bubbles render legibly, a tap opens the intended thread, the reel advances across captured frames and Back restores the right conversation. Measure cold start, warm capture latency, peak memory and capture size on the intended host. Record audio availability separately.

**To pass**, record before/after frames and verify correct targets for each supported action, including expected holds. If Obscura can render chats but only produces a reel poster, record separate chat and reel results. Browser selection stays open if it cannot support the required path. The older Playwright pipeline is historical implementation evidence.

## 3. Establish what Shadow can read

Run local OCR on labeled chat crops. Score literal text, order, sender, quotes and the association between a card and an accompanying message. On difficult cases, compare drafts made from OCR text with drafts made using the same text and the original crop in one main model request. Include Hinglish, emoji, clipped bubbles, repeated text and screenshots with multiple speakers.

For reels, compare how the smaller multimodal model summarizes the same clips with frames sampled at 1-, 2- and 4-second intervals. Include fast captions, a late reveal, an audio-dependent joke, a stalled player and an unavailable reel. Compare frames with a short clip containing audio when capture works. Check each summary against the reel, then have the owner judge the reply against the reel and the sender’s question. Record evidence missed, cost per turn and time to a usable draft.

**To pass**, identify when OCR supplies enough context and when an image improves the draft. Select the smaller reel model, reel sampling policy and main reply model from these results. Record which cases require more evidence or a hold.

## 4. Integrate a draft-only loop

Connect the qualified screen actions to Shadow’s coordinator. Reuse scoped memory, SQLite, pause, owner takeover and quality checks. Store only a draft during this phase. Exercise one complete turn from inbox through reel inspection and back to the original thread. Repeat with a new inbound message arriving during inference, an unknown contact, a model timeout and a process restart.

**To pass**, demonstrate that the draft belongs to the right contact and current turn, contains the relevant text and media context, and can be reviewed by the owner. Stale, ambiguous or unsupported turns end with a recorded reason. Count all attempted UI actions in the replay to verify the draft loop cannot send.

## 5. Evaluate assisted delivery separately

Measure which drafts the owner accepts, edits or skips. If assisted sending is pursued, reconnect the existing owner approval and outbox contracts only after visual recipient binding and confirmation have passed controlled checks. Exercise pause after approval, loss of confirmation and restart after one attempt. Keep automation scoped to the owner’s approved contacts and modes.

**To pass**, demonstrate that an approved fresh draft reaches the intended recipient once in the controlled case. Uncertain delivery survives restart without a duplicate. The owner decides whether evidence from an assisted pilot warrants any wider autonomy.

## Decisions to carry into implementation

| Topic | Current direction | When to settle it |
| --- | --- | --- |
| Browser interaction | Screenshots, coordinate input and post-action verification. Obscura is the first candidate. | Browser trial after the API test |
| Target selection | Prefer a small preset action vocabulary with calibrated fixed controls and per-frame targets for moving content. | Compare targeting errors and hold rates in phases 1–2 |
| Text extraction | Use local OCR and include the original crop in the main reply request when visual context matters. | Transcript, attribution and draft comparison in phase 3 |
| Reel context | Send captured media to a smaller multimodal model for interpretation. Give its summary and the associated chat to the main reply model. | Owner-rated reel comparison in phase 3 |
| Existing project | Keep the uv-managed Python project, local memory, voice work and owner controls. Replace the observation path incrementally. | Verify each reused contract during integration |

The [product requirements](prd.md) define the owner experience. The [architecture](architecture.md) assigns responsibilities. The [TRD](trd.md) defines the data, failure and verification contracts.

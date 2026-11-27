# Phase 1 navigation experiment

Question: on fresh synthetic Instagram fixtures, can Shadow open the correct unread conversation, open the reel from the latest incoming turn, close it, and return to that same conversation?

The browser tool is Vercel agent-browser 0.45.0 only. Playwright is not used. Screenshot `act()` stays the default. `--observe agent-nav` opts in through `tools.screenshot_turn`. No live Instagram session was opened. No message was sent, no reaction was posted, and no production timer was changed.

## Setup

| Item | Value |
| --- | --- |
| Starting commit | `4316109487257af462dc180a188b68fe80eac1bc` |
| Phase 1 commit | `ed5e24faa37e53335086639dcdab70ff7676b7ac` |
| Timing commit | `586a7020b4d12635200b77dab47a73a1bcb51407` |
| Phase 2 commit | `3d375496ec8059eee46232616d1e0aebfdb3afa4` |
| Host | macOS 27.0.1, arm64 |
| Browser | Google Chrome for Testing 155.0.8059.39 |
| agent-browser | 0.45.0, headed false, annotate false |
| Model | `gemini-decision-1` via `https://generativelanguage.googleapis.com/v1beta/decisions` |
| Viewport | 1280×720, device scale 1 |
| Confidence floor | 0.80 |
| Provider-call budget | 640 |
| Scoring | ceil(95% of the denominator): 60 → 57, 40 → 38, 20 → 19 |

Frozen before any live call: `tests/fixtures/nav_phase_frozen.json` (phase1 P001–P060 and reserve R001–R060) and `tests/fixtures/nav_phase_dev.json`. Expected answers stay in the harness. The historical misses H017–H022 and H041–H044 stay in `tests/fixtures/nav_heldout.json` and are not in either claim set.

Each destination is checked from the URL, the scoped snapshot, and the fixture's opened-id list. A numeric thread id that was not expected is a wrong thread. A hold succeeds only when nothing was opened. `close-photo` and ids that start with `photo` may appear once as recovery and do not by themselves pass a first attempt. The Decisions image is checked at the outgoing request boundary, including the local frame Phase 2 uses before a row click. Fidelity on every measured run was 0 invalid frames.

`shadowLocate`, `shadowToken`, and `shadowProbe` are not called by `shadow/nav_phase.py`. Dev case D006 deletes those hooks and Phase 2 still opened thread 41001, scored from the URL.

## Phase 1

Phase 1 asks Gemini which offered rows are unread, then Python applies the ordering rule. One low-confidence reel gets one crop and a yes/no question. The click is rechecked by thread id or accessible name, and the page is checked again after the click.

Scripted replay on P001–P060, same pages, alternating order: Phase 1 60/60 overall, 40/40 actionable, 20/20 journeys, 0 wrong-target clicks. The paired baseline was 46/60, 34/40, and 20/20, with 14 wrong-target clicks and 10 wrong threads. That replay shows the procedure, not the model.

Live Gemini on the same 60 cases, paced at 1.2 seconds:

| Procedure | Overall | Actionable | Journeys | First attempt | Second look | Wrong clicks | Wrong threads | Unnecessary holds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 40/60 | 20/40 | 15/20 | 40 | 0 | 0 | 0 | 20 |
| Phase 1 | 37/60 | 17/40 | 13/20 | 27 | 10 | 0 | 0 | 23 |

Phase 1 fails every accuracy bar (need 57, 38, and 19). It made no wrong-thread opening and no send.

Sixteen Phase 1 misses are two-unread inboxes. The model named the correct unread set and stayed under 0.80. On dev case D001 the returned choice was `c1+c2` at 0.424, so Python never ordered the rows. Single-unread and aria-unread rows cleared 0.80. Every reel hold that was not rate-limited held. Every journey that received a model answer succeeded.

Seven Phase 1 journey misses, and five baseline journey misses, are HTTP 429. The harness turned a journey `pending` into `hold` before the retry check, so those cases were not retried. They are misses in the table above. Even if all seven had succeeded, Phase 1 would still be 44/60 overall and 24/40 actionable because of the sixteen low-confidence inboxes.

Live latency on this set, monotonic clock, provider interval removed: Phase 1 end-to-end p50 1.135 s and p95 3.316 s; outside Gemini p50 0.201 s and p95 0.622 s. Baseline p50 1.053 s / p95 3.327 s end to end, and p50 0.277 s / p95 0.730 s outside Gemini. Some Decisions responses omitted token usage, so Phase 1 has no complete token total. The API did not return a dollar price. Fidelity: 183 frames, 0 invalid.

## Phase 2

The failed stage is the unread-set question, not ordering, binding, or the reel check. Phase 2 does not ask Gemini which rows are unread. A row is unread when its accessible name contains `unread` or the screenshot contains the indigo badge in that row. A name that says `read` while a badge is present is a conflict, and Shadow holds. Python then applies the same ordering rule: visible age, then an explicit newest/oldest banner, and the lower row only when the ages are equal. Missing ages are not treated as equal. Reels, the one second look, the close control, and the 0.80 floor are unchanged.

The opt-in entry point calls this procedure. Phase 1 remains in the module so the failed experiment stays reproducible.

Scripted replay on the untouched reserve, before the live claim: Phase 2 60/60, 40/40, 20/20, 0 wrong-target clicks, 202 frames, 0 invalid. The paired scripted baseline was 46/60 with 14 wrong-target clicks.

Live Gemini on R001–R060, alternating order, 2.0 second pace, one 30 second retry on HTTP 429. No retry was needed. Commit `3d375496ec8059eee46232616d1e0aebfdb3afa4`.

| Procedure | Overall | Actionable | Journeys | First attempt | After second look | Wrong clicks | Wrong threads | Calls | Input tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 45/60 | 25/40 | 20/20 | 45 | 0 | 0 | 0 | 98 | 147,099 |
| Phase 2 | 58/60 | 40/40 | 20/20 | 50 | 8 | 2 | 0 | 60 | 88,708 |

Need 57/60, 38/40, and 19/20. Phase 2 passes those three bars. It opened no wrong thread and sent nothing. Two wrong-target clicks remain: R055 and R056. Both are attachment screens whose correct action is a hold. The first reel decision chose the attachment at or above 0.80, so the second look did not run. The click did not open a thread or a player. Verification timed out. The harness counted the opened id. The baseline held on both, so Phase 2 lost those two cases.

Paired against the live baseline, Phase 2 gained 15 cases and lost 2. The gains are the two-unread inboxes the baseline held: four older-above, one older-below, two equal-age, two order-only, two aria-unread, two duplicate names, and two in-flight reorders. Journeys were 20/20 for both procedures. The eight second looks are correct holds on an older reel, an outgoing reel, a photo, or a link. Each used one extra call and about 1,400 extra input tokens. No reserve case needed the photo-preview recovery. Three proposals were intercepted before a click, all on holds that stayed correct.

Per successful Phase 2 task, including failed work: 1.03 model calls and 1,529 input tokens. A journey uses two calls and about 2,992 tokens because the row step does not call the model. The baseline used 2.18 calls and 3,269 tokens per successful task. The Decisions response did not include a dollar price.

Live latency on this reserve workload: Phase 2 end-to-end p50 0.934 s and p95 2.665 s; outside Gemini p50 0.274 s and p95 0.643 s. Baseline p50 1.206 s / p95 3.197 s end to end, and p50 0.292 s / p95 0.746 s outside Gemini. Provider time on the Phase 2 run summed to 46.58 s. Time outside Gemini summed to 27.32 s.

Scripted row timing on D001, n=100, same host, current clock, provider interval removed: baseline outside Gemini p50 0.333 s and p95 0.381 s; Phase 2 p50 0.218 s and p95 0.245 s. Phase 2 makes no provider call on that row, so its end-to-end time equals the outside time. Cold end-to-end, browser already constructed and the first navigation included, was 0.364 s for the baseline and 0.252 s for Phase 2. Process setup before launch was about 0.02 s.

Provider calls across the live runs, including the one D001 diagnosis call: 342. The budget was 640. The reserve set was not used to tune Phase 1.

## Verdict

Phase 1 did not pass. On the fresh P set the live score was 37/60 overall, 17/40 actionable, and 13/20 journeys. The model could name both unread rows and still scored 0.424.

Phase 2 is the method that met the bars. Unread rows come from the badge or the accessible name. Gemini still judges the reel and the close control. On the untouched reserve, live Gemini, the score was 58/60 overall, 40/40 actionable, and 20/20 journeys. Fifty of the sixty cases succeeded on the first look. Eight correct holds needed the second look. None needed navigation recovery. Two attachment cards were clicked and counted; neither opened a thread. No message was sent.

Outside Gemini, a scripted row is p50 0.218 s. On the live reserve mix, outside Gemini is p50 0.274 s and p95 0.643 s.

Shadow reached the goal on these synthetic fixtures.

## Commands

```text
uv run --locked --extra browser python -m unittest tests.test_nav_phase tests.test_nav_observe tests.test_screenshot_loop tests.test_screenshot_providers tests.test_vision_prompts
uv run --locked --extra browser python -m shadow.nav_phase
uv run --locked --extra browser python -m tools.nav_phase_benchmark --split dev --candidate phase2
uv run --locked --extra browser python -m tools.nav_phase_benchmark --split phase1 --output /tmp/nav-phase1-replay.json
uv run --locked --extra browser python -m tools.nav_phase_benchmark --split phase1 --live --output /tmp/nav-phase1-live.json
uv run --locked --extra browser python -m tools.nav_phase_benchmark --split reserve --allow-reserve --candidate phase2 --live --output /tmp/nav-phase2-reserve-live.json
uv run --locked --extra browser python -m tools.nav_phase_benchmark --split dev --limit 1 --candidate phase2 --timing --output /tmp/nav-phase2-timing.json
```

Opt in with `--observe agent-nav`. Omit it to keep the screenshot path. `SHADOW_AGENT_BROWSER` overrides the cache path. The binary must be agent-browser 0.45.0. Reserve stays unused unless `--allow-reserve` is passed.

After the Phase 2 commit, the focused tests were 68 passed and 2 skipped.

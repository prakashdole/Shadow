# Navigation observation pilot

Question: does a scoped accessibility snapshot from Vercel agent-browser 0.45.0, sent with the same screenshot to Gemini Decision 1, improve opening the correct unread conversation, opening its shared reel, closing the player, and landing back in that conversation?

Screenshot-only stays the default. `--observe playwright` and `--observe agent-browser` opt in through `tools.screenshot_turn` and through `act()`. Element-reference clicking was not added. No message was sent, no reaction was posted, and no production timer was changed.

## Setup

| Item | Value |
| --- | --- |
| Starting commit | `fbc22e9ef8bf5dbf1fabf4b57a0ebbdbd773df82` |
| Pilot commit | `53cc431c9a29ef54064e6cfbba1935de7bacb806` |
| Host | macOS 27.0.1, arm64 |
| Browser | Google Chrome for Testing 155.0.8059.39 |
| Playwright | 1.65.2, `locator.aria_snapshot(boxes=True)` |
| agent-browser | 0.45.0, pinned in `shadow/nav_observe.py`, installed outside the repo |
| Model | `gemini-decision-1` via `https://generativelanguage.googleapis.com/v1beta/decisions` |
| Viewport | 1280×720, device scale 1 |
| Confidence floor | 0.80 |
| Provider-call budget | 280 |

Arms share the task text, screenshot, legal choices, confidence floor, click path, and outcome check. B and C add one scoped snapshot. The id-to-target map stays in Shadow. Page text is untrusted. A click is allowed only when the observation proves one target, the tab and thread still match, and `shadowLocate` still hits that node. The fixture's `shadowOutcome()` scores the result. Expected answers are not in the page or the model request.

Frozen before tuning: `tests/fixtures/nav_heldout.json` (120 cases, 60 marked live, 20 marked reserve) and `tests/fixtures/nav_dev.json`.

## What failed once, then was fixed

The first held-out replay classified eight missing-label rows as `header_only` and waited. Those buttons had no painted text, so the existing contrast detector did not see them. The fixture now paints an aria-hidden placeholder. All three arms then hold, which is the frozen expected outcome. That fix was informed by those held-out cases. They do not separate the arms.

Playwright's cached connection missed the thread URL after `pushState`. It now reads `location.pathname` and reconnects once. A closed target used to drop the page session and abort the suite; the spare blank tab stays open and a dead session becomes a hold.

## Offline replay

Scripted transport, real Chromium, real screenshots, 120 cases × 3 arms. This is not the adoption number. The scripted policy can read the evidence line, so aria-only unread rows favor B and C by construction.

| Arm | Correct | Actionable | Journeys | Wrong actions | Unnecessary holds | Intercepted | Calls | Payload bytes | Useful evidence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A screenshot | 112/120 | 58/66 | 10/10 | 0 | 8 | 8 | 120 | 1,403,898 | 0 |
| B Playwright | 120/120 | 66/66 | 10/10 | 0 | 0 | 8 | 120 | 1,411,515 | 100 |
| C agent-browser | 120/120 | 66/66 | 10/10 | 0 | 0 | 8 | 120 | 1,409,580 | 76 |

A's eight misses are the aria-only rows. C's tool answered on all 120 cases. Aligned text was included on 76. The other 24 were visual or duplicate rows where the snapshot listed more rows than the unread filter offered, so no label was guessed. The dot still proved the row.

Screenshot check: 360 images at the request boundary, 0 invalid. Race: 6/6 per arm opened the original lower conversation after an in-flight reorder. `wrong_action` was 0. Detach, timeout, rate limit, malformed responses, confident wrong choices, stale replacement, wrong tab, and wrong thread all finished with no click.

Disconnect proof: with the agent-browser binary missing, the visual case still opened the dotted row, `evidence_source` was `screenshot_fallback`, and `fallback_reason` was `agent_browser_unavailable`.

## Latency gate

Matched warm replay of dev case D001. n=100 per arm. Clock is `time.monotonic`. The gate was frozen first: C's p95 time outside Gemini must be at most A's p95 plus the larger of 75 ms or 15% of A's p95.

| Arm | Cold end-to-end | Warm outside p50 | Warm outside p95 | Warm end-to-end p95 |
| --- | ---: | ---: | ---: | ---: |
| A | 168 ms | 143 ms | 157 ms | 157 ms |
| B | 399 ms | 175 ms | 209 ms | 209 ms |
| C | 3.30 s | 179 ms | 208 ms | 209 ms |

Allowance is 75 ms. Limit is 232 ms. C's 208 ms passes. B's 209 ms is beside it. Chromium process start was 473 ms and is not inside these samples. C's cold time is the agent-browser daemon (snapshot span 3.16 s). Warm snapshot p50 is 16 ms for C and 25 ms for B. Screenshot capture is the largest warm span, about 46–58 ms.

Delay proof, same case, 400 ms injected inside the provider call: end-to-end rose from 181 ms to 605 ms. Time outside Gemini rose from 180 ms to 200 ms.

## Real Gemini

Frozen live subset, 60 cases per arm, arm order rotated, fixture reset every case. The first pass hit HTTP 429 after the early cases (66 calls). Those calls were retried once, paced, and all 66 then returned a decision. No third attempt. Completed-call count on the final rows is 176. With the rejected attempts, the subset used 227 provider calls. Seven earlier probes bring the session to about 234, under the 280 budget. The 429 responses contained no token usage.

The first 161 live requests were checked at the boundary: 0 invalid. The 66 retries used the same capture path and were not checked again. No known-invalid image was sent.

| Arm | Correct | Actionable | Journeys | Wrong actions | Unnecessary holds | Calls | Input tokens | Tokens per success | Payload bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 38/60 | 22/44 | 0/4 | 0 | 22 | 56 | 82,938 | 2,183 | 676,422 |
| B | 50/60 | 34/44 | 4/4 | 0 | 10 | 60 | 89,579 | 1,792 | 720,820 |
| C | 50/60 | 34/44 | 4/4 | 0 | 10 | 60 | 89,268 | 1,785 | 719,568 |

95% would be 42/44 actionable and 57/60 overall. No arm reaches either bar. The API returned `total_input_tokens` and did not return a price.

B and C agreed on every live case. C solved 12 cases A missed: 8 aria-only unread rows and 4 journeys. C missed nothing A or B solved. The journey gap is the reel step. A opens the row, then holds. B and C open the reel and close it back into the same thread.

Shared misses, all holds, all with `wrong_action` false:

- Six two-unread rows. The model picked the lower row at about 0.55–0.65, under 0.80, on both A and C.
- Four isolated reel screens. A chose hold. B and C chose a card at about 0.69.

Live race: 4/4 per arm opened the original lower conversation. `wrong_action` was 0. Loading, missing labels, wrong tab, and wrong thread held or waited with no click.

Live case-level time outside Gemini, n=60, includes multi-step journeys and is not the gate: A p50 166 ms / p95 350 ms, B 191 / 600, C 191 / 639. Provider time sits inside the Gemini interval (p50 about 0.8 s).

## Verdicts

1. No. The agent-browser integration does not pass every required criterion. Action integrity, the offline race, the warm latency gate, fallback labeling, and the default screenshot path do. The frozen real-Gemini evaluation is 34/44 actionable and 50/60 overall, both under 95%. Unnecessary holds are the misses.

2. No. It does not earn adoption. Playwright matched it on all 60 live cases, so there is no gain over the best existing snapshot. Against screenshot-only it is higher by 12 paired cases (8 aria-only rows and 4 journeys), which is a real observation gain and not a one-case fluctuation. It does not win on speed: warm p95 outside Gemini is 208 ms versus 157 ms. Input tokens per successful task are 18% below screenshot-only and level with Playwright, short of the 20% bar. The Decisions response did not include a dollar price.

3. Live Instagram is unverified. This pilot never attached to a logged-in profile. On a page without the fixture's `shadowLocate` hook the cycle holds instead of clicking. Sending, reactions, and production timers were out of scope. Element-reference clicks were not tested. The 66 paced live retries were not rechecked at the image boundary. Dollar cost is unknown.

## Commands

```text
uv run --locked --extra browser python -m unittest tests.test_nav_observe tests.test_screenshot_loop tests.test_vision_prompts tests.test_screenshot_providers
uv run --locked --extra browser python -m tools.chromium_trial
uv run --locked --extra browser python -m tools.nav_benchmark --split heldout --timing --output /tmp/shadow-nav-timing.json
uv run --locked --extra browser python -m tools.nav_benchmark --split heldout --live --output /tmp/shadow-nav-live.json
```

Opt in from the normal entry point with `--observe agent-browser`. Omit it to keep the screenshot path. The binary must be agent-browser 0.45.0 (`SHADOW_AGENT_BROWSER` overrides the cache path).

After the code commit, the relevant checks were: 62 tests, 2 skipped (live provider tests), Chromium fixture trial passed, and `python -m shadow.nav_observe` passed.

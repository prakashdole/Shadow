# Screenshot draft runbook

The viewport is fixed at **1280×720**, device scale **1**. One turn ends at a saved draft. This entry point cannot send, approve delivery, or publish a reaction.

## Setup

From the repository root:

```sh
uv sync --locked --extra browser
```

Put keys in the git-ignored `.env`. `GEMINI_API_KEY` is required for Decision 1, transcription and the reply fallback. `GEMINI_API_KEY_2` and `GEMINI_API_KEY_3` are used only when a key is rejected. A 429 stays on the same key. `OPENAI_API_KEY` is the draft key; `SHADOW_OPENAI_API_KEY` still wins when it is set, and the older adapter is unchanged.

Chromium or Chrome must be on `PATH`, or set `CHROMIUM_BINARY`. The Instagram profile is `~/Library/Application Support/Shadow/chromium-instagram` on macOS. It is exclusive: quit `tools.chromium_viewer` before another process uses it.

## Checks

```sh
uv lock --check --offline
uv run --locked --extra browser python -m unittest discover -s tests
uv run --locked --extra browser python -m tools.chromium_trial
SHADOW_LIVE_PROVIDERS=1 uv run --locked --extra browser python -m unittest tests.test_screenshot_providers
```

## One synthetic turn

`tests/test_screenshot_loop.py` drives the fixture and checks the saved row. To inspect a draft from the command line, serve the fixture yourself and pass a **new** data directory, not the observer database:

```sh
uv run --locked --extra browser python -m tools.screenshot_turn \
  --data-dir "$HOME/Library/Application Support/Shadow/screenshot-drafts" \
  --fixture http://127.0.0.1:PORT/ --show-draft
```

## Live session

```sh
uv run --locked --extra browser python -m tools.screenshot_turn \
  --data-dir "$HOME/Library/Application Support/Shadow/screenshot-drafts" \
  --live
```

That reopens the profile and, without a model, opens inbox rows by verified position. It records whether a text thread loaded and whether a shared media card played and closed back to the same thread. It does not read display names. `--allow-live-model` sends the viewport to Gemini and OpenAI. Leave it off unless that project is approved for the private images.

## Measured locally

| Item | Result |
| --- | --- |
| Browser process ready | 0.485 s |
| One 1280×720 capture | 0.108 s |
| Inbox pixel-stable | not within 12 s. The page keeps moving, so a turn recaptures |
| Chrome process-tree RSS | 1,534,304 KB |
| Session after a full restart | inbox, not a login wall |
| Rows on that inbox | 5. The probe opened them by row index. Decision 1 did not see the screenshot |
| Plain DM | row 4 opened (`acted`), 6 text bands |
| Shared media in a DM | row 1 opened (`acted`). The player route was `p`, not `reel`. 8 frames, playback advanced, audio unavailable, close returned to the same thread |
| Public Reels playback | 4 of 4 frames changed on an earlier read-only pass. Audio was not captured |
| Synthetic draft | `gpt-4o-mini-2024-07-18`, 2725 ms, 246 tokens |
| Synthetic fallback | `gemini-3.5-flash-lite`, 2674 ms, 921 tokens, after the primary transport failed |
| Synthetic reel frames | `gemini-3.1-flash-lite`, 3521 tokens |
| Synthetic reel clip | `gemini-3.1-flash-lite`, 1361 tokens. Speech stayed null because the capture has no audio |

## Recovery

| What happened | What you get |
| --- | --- |
| Blank, header-only, or low confidence | `wait` or `hold`, then a fresh capture |
| Screen changed before the click | `target_moved`, no click |
| Login or challenge URL | runtime `pause_reason=login_required` |
| Owner pause or takeover | the turn returns `paused` or `owner_active` |
| HTTP 429 or a daily quota | the turn stays `pending` on the same key |
| Primary draft transport failure | one Gemini 3.5 Flash-Lite fallback; a model `hold` is kept |
| New message while drafting | the reply is discarded and the turn stays eligible |
| Restart | the same revision returns the existing draft; an older revision stays `superseded` |

Pause the observer with `uv run --locked shadow pause` when that process is the one running. The screenshot directory is separate.

## Host notes

A future Linux host needs Chromium, a private persistent profile directory, SQLite on local disk, the same environment variables, and a supervisor that runs one process at a time against that profile. Give the browser at least 2 GB. The profile lock is exclusive. Do not deploy from this runbook.

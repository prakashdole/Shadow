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

This browser-only probe reopens the profile, opens existing inbox rows by verified position, and checks whether a text thread loads and shared media plays. Opening a thread can mark it as read. The probe skips model requests.

To check one old conversation, pass `--thread THREAD_ID` with the numeric ID from its Instagram URL. A redirected thread fails verification. With `--allow-live-model`, a targeted turn sends the verified conversation crop and applicable reel frames to Gemini and sends structured transcript and media context to the reply model. An untargeted turn also uses the inbox screenshot to select a visibly unread row. Gemini API projects used for private messages, including backup keys, need active Cloud Billing. [Gemini API terms](https://ai.google.dev/gemini-api/terms)

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
| Shared media in a DM | Rows 1, 2, 3, and 5, plus the top row after three list scrolls, all opened a `p` player. Playback advanced. The close returned to the same thread for row 1, row 5, and the scrolled rows. No `/reel/` route appeared |
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
| New message while drafting or changed thread | the reply is discarded |
| Owner already answered | the old draft is superseded and the turn holds |
| Retry after opening an unread row | its exact thread ID survives restart for up to three attempts total |
| Restart on the same revision | the existing draft is reused |

After signing in again, run a browser-only check with `--live --resume-login` to clear a login pause for this account and data directory. Stop the timer to pause scheduled screenshot turns.

## Release evidence

The signed-in Mac session survived a restart. An old plain thread opened, and an old shared video played in Instagram's post viewer. The canonical reel URL matched the same media ID. The browser probe confirmed playback without sending chat data to a model.

The synthetic browser test runs a plain chat and a shared reel through Chromium, Gemini Decision 1, transcription, optional reel framing, and a saved GPT draft. It checks the stored media summary and an empty outbox. Run it with the rest of the real provider checks via `SHADOW_LIVE_PROVIDERS=1`. The Linux ARM64 host passed all 40 tests with real provider calls enabled against synthetic inputs, including the plain and reel browser-to-draft flows. A supervised Chrome smoke test and the private SSH login viewer passed on that host. Real private-message drafts still require a verified billing-enabled Gemini project and a signed-in Linux profile.

## Server bring-up

1. From the Mac checkout, upload the selected revision's source only. The repository also holds local data, so use the explicit paths in the archive. Choose a new release path for each revision.

   ```sh
   release=$(git rev-parse --short HEAD)
   git archive HEAD pyproject.toml uv.lock shadow tools tests deploy |
     ssh shadow-host "umask 077; mkdir -p ~/.local/share/shadow-releases/$release &&
       tar -xf - -C ~/.local/share/shadow-releases/$release"
   ```

2. On `shadow-host`, enter that release and run `uv sync --locked --extra browser`, then run the checks above. Allow at least 2 GiB for Chrome. Use the non-root SSH account.
3. Run `uv run --locked --extra browser python -m tools.chromium_viewer` in the release over SSH. It prints a private loopback URL. Open a second SSH session from the Mac with `ssh -N -L 127.0.0.1:REMOTE_PORT:127.0.0.1:REMOTE_PORT shadow-host`. Use the printed port for both ends, then open the original URL on the Mac. Sign in directly in the viewer, then choose **Save session and close viewer**. The Linux profile remains under `~/.local/share/shadow/chromium-instagram`. Use the read-only `--live` probe to verify the session after a browser restart.
4. Put only approved provider keys and the chosen `SHADOW_ACCOUNT_ID` in `~/.config/shadow/screenshot.env` with mode `0600`. For the manual smoke test, export those variables into the test shell without echoing them. Use `--data-dir "$HOME/.local/share/shadow/screenshot-drafts" --live --allow-live-model --thread THREAD_ID` for an approved existing plain chat and an approved shared reel chat. Verify both produce the expected draft or a correct hold if the owner already answered. Check the result has zero sends and reactions.
5. After the tests pass, point `~/.local/share/shadow-release` at the qualified release and install both files from `deploy/` into `~/.config/systemd/user/`. Run `systemctl --user daemon-reload` and start `shadow-drafts.service` once. Inspect `journalctl --user -u shadow-drafts.service` for the result.
6. To run after logout or reboot, enable linger for the SSH user with `loginctl enable-linger "$USER"` and start `shadow-drafts.timer` with `systemctl --user enable --now shadow-drafts.timer`. The timer runs one turn two minutes after the last one exits, without overlap. Disable it with `systemctl --user disable --now shadow-drafts.timer`. Then run `systemctl --user stop shadow-drafts.service` before changing the profile or release link.

SQLite and the browser profile live outside the release directory. The service uses a private umask and writes only results and identifiers to its journal. Omit `--show-draft` from scheduled work. Draft content stays in SQLite for review.

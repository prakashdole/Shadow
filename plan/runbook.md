# Instagram inbox runbook

The viewport is fixed at **1280×720**, device scale **1**. A manual turn can stop at a saved draft. The scheduled check replies once to an eligible unread chat and confirms the send from a freshly loaded Instagram document.

India daytime is **06:00 through 23:59 Asia/Kolkata**. The timer fires every 10 minutes from **06:00 through 23:50** and triggers one catch-up check after downtime if it missed a slot. The worker opens the inbox and sends only between **06:00 and midnight**. A submission that already started can finish its reload check.

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

Delivery clicks the visible Send control once and confirms the message after reopening the exact conversation. The database records the attempt before the click. An optimistic bubble in the current page is not confirmation. If the message cannot be confirmed, the outbox item stays `uncertain` and is not sent again. The next in-hours check reloads that thread and confirms the item only when the fresh document shows exactly one copy.

The scheduled unit is `shadow-drafts.service`, started by `shadow-drafts.timer`. It runs `tools.screenshot_turn --live --allow-live-model --deliver`. One check handles at most three chats, starting with unfinished opened threads and then the oldest visibly unread row. Any unread personal or group chat in the inbox can receive a reply on its first visit. The worker still skips stories, reaction-only events, and conversations the owner has paused. A second process exits as `overlap` because the database lock is exclusive.

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
| Same reel turn after playback, aside from whitespace, a dropped reel label, or a Seen line | the draft can still be sent |
| Owner already answered | the old draft is superseded and the turn holds |
| Retry after opening an unread row | the thread stays queued even if the unread badge is gone, for up to three attempts |
| Restart during `attempting` | the item becomes `uncertain` and is reconciled, not resent |
| Outside 06:00–23:59 Asia/Kolkata | `outside_hours`, no inbox open and no send |

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
5. Point `~/.local/share/shadow-release` at the qualified release and install both files from `deploy/` into `~/.config/systemd/user/`. Run `systemctl --user daemon-reload`. Do not enable the timer until you want live sends.
6. Linger lets the user service manager survive logout and reboot: `sudo loginctl enable-linger "$USER"`. The production timer stays disabled until you turn it on.

## Operate

Turn the schedule on:

```sh
systemctl --user enable --now shadow-drafts.timer
```

Stop it:

```sh
systemctl --user disable --now shadow-drafts.timer
systemctl --user stop shadow-drafts.service
```

The next check, without starting anything:

```sh
systemctl --user list-timers shadow-drafts.timer
systemd-analyze calendar --iterations=3 '*-*-* 06..23:00/10:00 Asia/Kolkata'
```

Logs, identifiers and outcomes only:

```sh
journalctl --user -u shadow-drafts.service -n 80 --no-pager
```

Uncertain send: do not click Send again and do not delete the row. The next in-hours check reloads the thread. Confirm it only from that fresh document. See the state without the message text:

```sh
sqlite3 "$HOME/.local/share/shadow/screenshot-drafts/shadow.sqlite3" \
  "SELECT id, conversation_id, state FROM outbox WHERE state != 'confirmed';"
```

Stop the timer before changing the profile or the release link. Qualification runs use `--allow-thread THREAD_ID` so other open chats are not transcribed or sent. The installed service does not pass `--allow-thread`.

SQLite and the browser profile live outside the release directory. The service uses a private umask and writes only results and identifiers to its journal. Omit `--show-draft` from scheduled work. Draft content stays in SQLite.

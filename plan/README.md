# Shadow

Shadow is a self-hosted assistant for one owner’s Instagram conversations. The next version will read the visible chat, interpret shared reels and draft a reply in the owner’s voice. Its first experiment uses saved screenshots. The current checkout still runs the earlier Python prototype.

| Read this | When you need to know |
| --- | --- |
| [Product requirements](prd.md) | Which conversations Shadow handles, what the owner sees and what counts as a good draft |
| [Architecture](architecture.md) | How the screenshot, browser, Decision 1, OCR, reply model and local state fit together |
| [Technical requirements](trd.md) | What each component receives and returns, when a tap is valid and how failures are handled |
| [Implementation plan](plan.md) | What to build first, how to test it and which decisions are still open |

## Run the existing CLI

Run these commands from the repository root.

```sh
uv sync --locked
uv run --locked shadow --help
```

The previous test suites were removed. The implementation plan defines the first replacement checks. Keep API keys in the ignored local environment. The checkout and its history contain private conversations and derived voice data.

## Run the Obscura trial

Use the render-enabled Obscura build. The trial starts a loopback browser session, serves a synthetic inbox and verifies what each click changed. The checked-in fixture supplies colored markers for target detection.

```sh
uv run --locked --extra obscura python tools/obscura_trial.py \
  --browser "$HOME/.local/opt/obscura-0.2.7/obscura" --with-model
```

Use `--with-model` to score screenshots with Gemini Decision 1 using `GEMINI_API_KEY`. Omitting it exercises the browser with predetermined choices. Use `--screenshots` with a new directory to retain the synthetic frames.

The macOS ARM64 build rendered Instagram's sign-in screen. Live DMs require an authenticated Obscura session. The synthetic reel moved between frames, while separate local MP4 and WebM probes failed to advance playback.

## Sign in to Obscura

```sh
uv run --locked --extra obscura python tools/obscura_login.py
```

Open the localhost address printed by the command. Click inside Instagram's preview, enter your login there and complete any verification. When your inbox is visible, use **Save session and close viewer**. The viewer forwards inputs locally and has no model calls. Use the same command to reopen the same profile.

Obscura keeps cookies and local storage under `~/Library/Application Support/Shadow/obscura-instagram`. Instagram can still expire your session. Browser session storage belongs to the open tab, and the profile has no verified persistent HTTP cache. [Obscura storage documentation](https://github.com/h4ckf0r0day/obscura/blob/main/docs/Persist-cookies-and-storage.md)

### Sign in through Chrome on the same Mac

Close the Obscura viewer first. Run the pairing helper with the path to a Chrome or Chrome for Testing executable.

```sh
uv run --locked --extra obscura python -m tools.obscura_pair \
  --chrome "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

Sign in on Instagram in the separate Chrome window within five minutes. Shadow transfers Instagram cookies from that temporary browser profile over loopback and checks the inbox route in a temporary Obscura profile before saving to the regular one. This flow uses no model calls. Instagram may reject the session in Obscura even when Chrome is signed in.

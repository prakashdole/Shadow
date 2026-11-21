# Shadow

Shadow is a self-hosted assistant for one owner’s Instagram conversations. The Chromium viewer and screenshot trial are available now. The existing CLI runs the earlier Python prototype. Reading live chat, interpreting shared reels and drafting a reply in the owner’s voice are the next integration steps.

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

## Run the Chromium trial

Install Chromium or Chrome for Testing on the host. Shadow looks for Chromium or Chrome on PATH and can use an existing Chrome for Testing installation on macOS. Pass `--browser /path/to/chromium` or set `CHROMIUM_BINARY` to choose the executable.

```sh
uv run --locked --extra browser python -m tools.chromium_trial --with-model
```

The trial serves a synthetic inbox and verifies what each screenshot-driven click changed. The fixture supplies colored markers for target detection. Omit `--with-model` to run offline. Use `--screenshots` with a new directory to save synthetic frames.

## Open the Chromium viewer

```sh
uv run --locked --extra browser python -m tools.chromium_viewer
```

Open the printed local address. Use **Inbox** and **Reels** to switch pages and **Save session and close viewer** to stop the browser gracefully. The viewer shows Chromium’s actual viewport and runs without model calls. On a desktop, add `--headed` to sign in through Chromium’s own window using the same profile.

The profile defaults to `~/Library/Application Support/Shadow/chromium-instagram` on macOS and `${XDG_DATA_HOME:-$HOME/.local/share}/shadow/chromium-instagram` on Linux. On a server, use `--profile` to point to a private persistent volume. Chromium runs headless by default and exposes the viewer and DevTools connection on loopback. [Chrome Headless documentation](https://developer.chrome.com/docs/automation-and-testing/headless/)

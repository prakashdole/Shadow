# Shadow

Shadow is a self-hosted Instagram DM assistant. It uses a persistent Chromium profile and screenshots to find unread chats, understand text and shared reels, and generate replies.

| Task | Model |
| --- | --- |
| Choose inbox and reel controls | `gemini-decision-1` |
| Extract visible conversation text | `gemini-3.5-flash-lite` |
| Summarize sampled reel frames | `gemini-3.1-flash-lite` |
| Write the reply | `gpt-4o-mini` |
| Write the reply if the main provider fails | `gemini-3.5-flash-lite` |

Install `uv` and Chromium or Chrome, then run:

```sh
uv sync --locked --extra browser
uv run --locked --extra browser python -m tools.screenshot_turn --help
uv run --locked --extra browser python -m unittest discover -s tests
```

Copy `.env.example` to `.env` for local model credentials. The repository includes a generic reply guide and fictional test fixtures. Keep credentials, browser profiles, chat archives, personal reply guides, and the worker database on your own machine or server.

See the [runbook](plan/runbook.md) for signing in, running draft checks, and enabling the Linux systemd timer. See the [architecture](plan/architecture.md) for the screenshot pipeline and [product requirements](plan/prd.md) for the intended DM workflows.

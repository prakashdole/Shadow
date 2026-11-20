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

# Tests

Standard-library `unittest` only. No network, no credentials, no model API, no
owner data. The offline suite needs no browser; the browser suite drives a
headless Chromium against fictional `file://` fixtures.

## Run the offline suite

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## Run the browser suite (P02 onwards)

```sh
python3 -m unittest discover -s tests/browser -p 'test_*.py' -v
```

It launches a real headless Chromium and runs the **production** reader
JavaScript from `shadow/dom.py` against the fictional fixtures in
`tests/fixtures/dom/`. There is no mock of the parser: a test that passes here
means the real extractor produced the structure the real interpreter consumed.

**Skip behaviour:** if Chromium cannot launch, the browser tests `skip` with the
exact reason. A skip is never counted as a pass and never silent — if the
browser suite is entirely skipped, that is a missing gate, not a green one.

The browser suite is slower than the offline one (each test starts a browser
context). Run it before every commit, but the offline suite alone is enough to
iterate on `shadow/dom.py`'s interpretation rules.

## Run the legacy entry point

`test_shadow.py` is a thin compatibility shim. It runs the offline suite and then
the browser suite, and exits non-zero if either fails.

```sh
python3 test_shadow.py
```

## Run one area

```sh
python3 -m unittest discover -s tests -p 'test_dom_interpret.py' -v
python3 -m unittest discover -s tests/browser -p 'test_reader.py' -v
```

## Public vs private checks

**Public — safe for CI and for a clean copy, no private files:**

| Command | Area |
| --- | --- |
| `python3 -m unittest discover -s tests -p 'test_*.py' -v` | the offline suite |
| `python3 -m unittest discover -s tests/browser -p 'test_*.py' -v` | the browser fixture suite |
| `python3 tools/privacy_audit.py --summary` | A01/A02 private-artifact counts |
| `python3 tools/privacy_audit.py --history` | A02, includes every path ever committed |

**Private — need the owner's corpus or a logged-in browser, never in CI, never
committed:**

| Script | Needs |
| --- | --- |
| `tools/private_checks.py` | the real `data/people/` tree |
| `tools/dom_discovery.py validate` | the owner's logged-in dedicated browser on loopback |
| `build_voice.py` | `voice.json` output and the real corpus |
| `make_eval_set.py` | the real corpus |
| `eval_voice.py` | `eval_cases.json`, `voice.json`, a chosen model + credentials |

`tools/private_checks.py` prints PASS/FAIL and counts only — never a contact
name, folder name, message or handle. `tools/dom_discovery.py validate` runs the
production reader on whatever the owner has open, printing statuses and counts
only; it clicks, types, scrolls and sends nothing. A private check failing is a
finding about the environment or the corpus, not a public test failure.

## Fixture rules

- Fixtures live under `tests/fixtures/<area>/` and are **written from scratch**.
- Content is fictional. Never copy a real conversation and rename it; a renamed
  real chat is still real chat, including its facts, timing and phrasing.
- Names, handles and handles-of-fictional-creators must read as obviously fake
  (`test_friend`, `Fictional_Person`, `@fictional_creator`).
- No real credentials, tokens, keys, IDs, host paths or account identifiers,
  including in comments, docstrings, assertion messages and test names.
- A test may build a canary at runtime (`"AK" + "IA" + "Q" * 16`) so the test
  file itself never contains a secret-looking literal.
- Fixtures are committed. Real data never is.

## Test layout

- `tests/test_<area>.py` — one file per area, named to match the `-p` pattern.
  These run with no browser.
- `tests/browser/test_<area>.py` — browser-fixture tests, introduced in P02.
- **No `__init__.py` anywhere.** Each discovery run uses its own directory as
  the top-level directory, so test modules import as top-level modules.
- `tests/fixtures/<area>/` — synthetic fixtures for that area.
  `tests/fixtures/dom/` holds fictional Instagram-shaped pages plus
  `FINDINGS.md`, the sanitized live-DOM evidence they are built from.
- Tests that need a data directory build one with `tempfile.mkdtemp()`, point
  the module under test at it, restore the original in `tearDown`, and remove
  it. Never write to the real data directory.
- Assertions read settings by name from the settings module, never from copied
  numeric literals.

## Verify a clean copy

The public suites must pass with no `data/`, `voice.json` or `.env` present:

```sh
TMP=$(mktemp -d)
cp -R shadow tests tools test_shadow.py pyproject.toml "$TMP"/
(cd "$TMP" && python3 -m unittest discover -s tests -p 'test_*.py' -v \
              && python3 -m unittest discover -s tests/browser -p 'test_*.py' -v)
rm -rf "$TMP"
```

## Current known state

`python3 tools/privacy_audit.py --history` **fails on this checkout** (exit 1).
That is the recorded release blocker, not a broken test: private corpus, voice,
persona, evaluation and generated-graph paths are already tracked and already
committed. Ignore rules only protect new untracked files. Full output stays
outside the repository; the repo records counts only.

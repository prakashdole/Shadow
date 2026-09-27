# Tests

Standard-library `unittest` only. No network, no browser, no credentials, no
model API, no owner data.

## Run the public suite

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## Run the legacy entry point

`test_shadow.py` is a thin compatibility shim. It discovers the same suite under
`tests/` and exits with its status.

```sh
python3 test_shadow.py
```

## Run one area

```sh
python3 -m unittest discover -s tests -p 'test_privacy_audit.py' -v
```

## Public vs private checks

**Public — safe for CI and for a clean copy, no private files:**

| Command | Area |
| --- | --- |
| `python3 -m unittest discover -s tests -p 'test_*.py' -v` | everything below |
| `python3 tools/privacy_audit.py --summary` | A01/A02 private-artifact counts |
| `python3 tools/privacy_audit.py --history` | A02, includes every path ever committed |

**Private — need the owner's corpus, never in CI, never committed:**

| Script | Needs |
| --- | --- |
| `tools/private_checks.py` | the real `data/people/` tree |
| `build_voice.py` | `voice.json` output and the real corpus |
| `make_eval_set.py` | the real corpus |
| `eval_voice.py` | `eval_cases.json`, `voice.json`, a chosen model + credentials |

`tools/private_checks.py` prints PASS/FAIL and counts only — never a contact
name, folder name, message or handle. A private check failing is a finding about
the corpus, not a public test failure.

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
- **No `tests/__init__.py`.** Discovery runs with `tests/` as the top-level
  directory, so test modules import as top-level modules.
- `tests/fixtures/<area>/` — synthetic fixtures for that area.
- Tests that need a data directory build one with `tempfile.mkdtemp()`, point
  the module under test at it, restore the original in `tearDown`, and remove
  it. Never write to the real data directory.
- Assertions read settings by name from the settings module, never from copied
  numeric literals.

## Verify a clean copy

The public suite must pass with no `data/`, `voice.json` or `.env` present:

```sh
TMP=$(mktemp -d)
cp -R shadow tests tools test_shadow.py pyproject.toml "$TMP"/
(cd "$TMP" && python3 -m unittest discover -s tests -p 'test_*.py' -v && python3 test_shadow.py)
rm -rf "$TMP"
```

## Current known state

`python3 tools/privacy_audit.py --history` **fails on this checkout** (exit 1).
That is the recorded release blocker, not a broken test: private corpus, voice,
persona, evaluation and generated-graph paths are already tracked and already
committed. Ignore rules only protect new untracked files. Full output stays
outside the repository; the repo records counts only.

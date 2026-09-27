# Requirement to evidence matrix

One row per requirement in [../plan/prd.md](../plan/prd.md). The middle column
names the test IDs from [../plan/testing.md](../plan/testing.md) that cover the
requirement: A (privacy and clean installation), S (storage/identity/migration),
B (DOM parser), D (sending and reconciliation), C (coordinator and pacing),
T (Telegram control), P (provider and policy), M (adaptation and portability),
Q (conversational evaluation).

Status reflects what exists at this revision, not what is intended. Each phase
updates the rows it implements; P09 verifies the whole matrix at the release
candidate rather than creating it.

Implemented by P00: A01, A02, and the offline part of A03. A04 (release-candidate
allowlist and history review) stays with P09; A05 (diagnostics and CI upload
rules) with P08/P09. S, B, D, C, T, P, M and Q have no implemented evidence yet.

| ID | Requirement | Test IDs | Status |
| --- | --- | --- | --- |
| PR-01 | Instagram access uses rendered DOM only | B05, B09 | pending (P02) |
| PR-02 | Respond to incoming one-to-one DMs | C10, C11, B02 | pending (P03) |
| PR-03 | Check real conversation before replying | B02, B06, D06, C04 | pending (P02/P03) |
| PR-04 | Natural voice | Q, P07 | pending (P05/P07) |
| PR-05 | Relationship-specific style | M04, Q | pending (P06/P07) |
| PR-06 | Meaningful portable memory | M01-M05, M07 | pending (P06) |
| PR-07 | Occasional correction learning | M01, M02, M09, T06 | pending (P06) |
| PR-08 | Contextual risk handling | P04, P05, P06, Q | pending (P05/P07) |
| PR-09 | Owner notifications | T05, T07 | pending (P04) |
| PR-10 | Owner commands | T01-T04, T08 | pending (P04) |
| PR-11 | Durable send truth | D01-D05, D07, D08 | pending (P03) |
| PR-12 | Portable model configuration | M06, P02, P03 | pending (P05/P06) |
| PR-13 | Private-by-default distribution | A01, A02, A03; A04 pending (P09) | implemented: tests/test_privacy_audit.py, tests/test_legacy_behaviour.py (A01, A02, offline part of A03) |
| PR-14 | Operable self-hosting | A05, soak/recovery gates | pending (P08) |
| PR-15 | Safe stopping and dry runs | T03, C05, C07 | pending (P03/P04) |
| PR-16 | Routine owner context | M03 | pending (P06) |
| PR-17 | Staged modes | C10, D09, T08 | pending (P03/P04) |
| PR-18 | Sent-message review | T07 | pending (P04) |
| PR-19 | Natural presence | C01, C07, C08, C09 | pending (P03) |
| PR-20 | Live quality guard | P09, P07 | pending (P05) |

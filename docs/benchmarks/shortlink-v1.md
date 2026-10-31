# Benchmark: shortlink v1 (wave #18)

The first end-to-end run of autonome on a real project, kept as the baseline for the next version.
Rerun the same prompt on a fresh shortlink scaffold and compare against the numbers here.

- **Date**: 2026-10-31, waves run 11:06–13:21 (-03:00) on shortlink's main.
- **autonome**: from `fec0ca8` (waves) to `be5d7e6`; bugs found by this run were fixed while it ran
  (see "What the process got wrong").
- **Models**: every conduct role on the account default (Opus, effort high); handoff and docs
  scribes on Sonnet. Per-role models did not exist yet.
- **Raw data**: [`shortlink-v1.json`](shortlink-v1.json) (wave, plans, tasks with runs, gate runs,
  review findings, handoff friction, tune summary).

## The prompt

The starting point: the `0e481ce` scaffold of shortlink (FastAPI, stdlib sqlite3, uv, ruff and
pytest, a `/health` route and its test) and this brief to a compose session:

> Use the `autonome:compose` skill to compose a **wave** for project `shortlink` together with the
> user. The wave has two features: (1) `links`: shorten a URL (`POST /links` creates a short code)
> and redirect (`GET /{code}`), with URL validation; (2) `click-stats`: count accesses per link and
> expose `GET /links/{code}/stats`; it depends on `links`. Follow the skill: search the memory, read
> the repo, check `gate_config_get`, grill one question at a time with a recommendation, then draft
> one plan per feature with `plan_draft` and group them with `wave_draft`.

The user's answers during compose, as recorded in the wave:

- Out of scope for the wave: auth, custom aliases, expiration, edit/delete, rate limiting.
- Asked for more gates (style, linter) since the goal is to validate autonome working under limits.
- Accepted a separate first feature for them: three features, `quality-gates` → `links` → `click-stats`.

Gate config at the start: `ruff`, `pytest` (from the scaffold); the wave added `format`, `types`
(mypy strict), `imports` (import-linter contracts) and `coverage` (≥ 90 %, branches). No soundcheck.

## Results

| | |
|---|---|
| Features / tasks | 3 / 9 (4 + 3 + 2), all merged |
| Sessions | 12 (3 tasks needed more than one) |
| Gate runs / failures | 62 / 2 |
| Review rounds | 11 (one task used 3) |
| Tokens / estimated cost | 395 k / US$ 6.55 |
| Wall time | ~2 h 15 min, most of it lost to autonome bugs; agent time ~15 min |
| Human interventions | 3: two retries in the UI (#23, #25) and one manual fix of a gate rule (`c90c07f`) |
| Final state | 30 tests, 100 % coverage, every gate green |

| Task | Feature | Sessions | Review rounds | Gate runs (failed) | Tokens | Outcome |
|---|---|---|---|---|---|---|
| #21 stricter ruff + format | quality-gates | 1 | 1 | 2 (0) | 39 k | merged |
| #22 mypy strict | quality-gates | 1 | 1 | 3 (0) | 21 k | merged |
| #24 import-linter contracts | quality-gates | 1 | 1 | 4 (0) | 20 k | merged |
| #23 coverage with pytest-cov | quality-gates | 2 | 3 | 13 (0) | 57 k | blocked, then merged |
| #25 SQLite connection and fixture | links | 3 | 1 | 16 (2) | 115 k | error, blocked, then merged |
| #26 POST /links | links | 1 | 1 | 6 (0) | 44 k | merged |
| #27 GET /{code} | links | 1 | 1 | 6 (0) | 33 k | merged |
| #28 count clicks | click-stats | 1 | 1 | 6 (0) | 32 k | merged |
| #29 GET /links/{code}/stats | click-stats | 1 | 1 | 6 (0) | 33 k | merged |

## Code quality (review of the merged code)

Followed the plans closely: one responsibility per module, parameterised SQL, atomic click upsert,
302 not 301, the catch-all route last, tests beyond the brief (malformed IPv6, exact 2048-char
limit, giving up after 5 collisions). Gaps nobody asked for:

1. URL validation accepts control characters (`http://ex.com\x00x`).
2. `REFERENCES links(code)` is decorative: SQLite runs with `foreign_keys` off.
3. Five code collisions raise `RuntimeError`: the client gets a raw 500.
4. Responses are dicts, not response models (a poor OpenAPI).
5. Some tests exist only for coverage (`test_db_path_defaults`, `test_get_conn_closes_connection`).
6. Product decisions never asked: `localhost`, private IPs and `user:pw@` are accepted (open redirect).

The reviewer of the time blocked none of them: shortlink had no conventions, and it judged the diff
against the plan only. Rerun on the `POST /links` commit with conventions (`autonome bench`), the
new reviewer finds 1, 3 and 4.

## What the process got wrong

| Problem | Cost | Fixed in |
|---|---|---|
| A generated `.coverage` got staged and the implementer could not unstage it | #23 blocked, 2 review rounds | `7659966`, `699a873`, `a46a6ef` |
| Plan #15's import contract could never pass plan #16's own spec | #25 blocked, manual fix `c90c07f` | `be5d7e6` |
| A long plan overflowed the embedding model's context: rehearsal returned 500 | #25 error | `ed6bff0`, `eaa7065`, `1cbb313` |
| Retry lost while the context restarted; conduct exited after a merge | 2 manual restarts | `61f7c8d`, `4b0a094` |
| Commit subjects lowercased acronyms (`pOST /links`) | cosmetic | `930eefe` |
| Handoffs filed under worktree-named projects: memory, insights and tune blind | no learning from the wave | `fe16fea` |
| Every role on Opus with high effort | most of the cost | `3a8a582` |

## Learned (tune, run after the fact)

Six conventions proposed from this wave's evidence, linked to it: write files with Edit/Write and
not Bash (denied in 8 of 11 sessions), read the briefing first, run lint-imports and coverage before
finishing, ignore generated files before generating them, every domain error maps to a JSON detail,
pin Python 3.13.

## How to compare the next version

Same scaffold (`0e481ce`), same brief, same answers, same gates. Compare: tasks blocked, human
interventions, sessions per task, review rounds, gate failures, tokens and cost, wall time, and how
many of the six gaps above are still in the merged code.

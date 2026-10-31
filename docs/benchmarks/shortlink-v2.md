# Benchmark: shortlink v2 (wave #102), against v1

Same scaffold (`0e481ce`), same brief, the same answers as far as the questions allowed, same
starting gates (ruff, pytest). What changed is autonome: conventions proposed by plans, an edge-case
pass in compose, a reviewer that judges conventions and edges, approval in the session, per-role
models (musician Sonnet medium, reviewer and rehearsal Haiku low), the stray-file check, the
restart-safe maestro. Raw data: [`shortlink-v2.json`](shortlink-v2.json); v1:
[`shortlink-v1.md`](shortlink-v1.md).

Run on 2026-10-31, 15:00–15:52 (-03:00), autonome at `61916eb`.

## Process

| | v1 | v2 |
|---|---|---|
| Features / tasks | 3 / 9 | 2 / 10 (the gates went into the links plan) |
| Blocked tasks | 2 (artifact in commit; gate rule vs plan) | 1 (the account's usage limit, not the work) |
| Human interventions | 3 (two retries, one manual gate fix) | 1 (one retry after the limit reset) |
| Sessions | 12 | 11 |
| Review rounds | 11 (3 on one task) | 11 (1 each) |
| Gate runs / failures | 62 / 2 | 55 / 1 |
| Tokens | 395 k | 427 k |
| Estimated cost | US$ 6.55 | **US$ 1.49** (−77 %, Sonnet instead of Opus) |
| Agent time | ~15 min | 11.6 min |
| Wall time | ~2 h 15 min (autonome bugs) | 52 min (including the usage-limit pause) |
| Tests / coverage | 30 / 100 % | 70 / 99 % |
| Conventions | none | 5 proposed by the plan and active; 3 more proposed by tune |
| Product questions asked | none | internal hosts (refused), scope; DNS resolution put out of scope explicitly |

## Code: the six v1 gaps

Checked on the merged code by calling the API (`TestClient`) and reading it.

| v1 gap | v2 | Evidence |
|---|---|---|
| Control characters accepted | **partly fixed** | NUL and other C0 characters and DEL → 400. C1 (`\x85`) and a space inside the URL are still accepted. |
| Foreign keys not enforced | **fixed** | `PRAGMA foreign_keys = ON` in `connect()`; an orphan click is refused. |
| Raw 500 after exhausted retries | **not fixed** | Five collisions still raise `RuntimeError` → 500. The active convention forbids it; the reviewer saw it and rated it low. A failed click write in the redirect is a second raw-500 path (reviewer: medium). |
| Dicts instead of response models | **fixed** | `LinkOut`, `StatsOut`; validation errors return a string detail. |
| Tests only for coverage | **mostly fixed** | Tests assert behaviour on a temp database; `test_init_db_is_idempotent` asserts nothing and `test_db_path_defaults_without_env` remains. |
| Product decisions never asked | **fixed, with a hole** | Internal hosts were asked and are refused (loopback, private, link-local, IPv4-mapped). Alternative spellings (`http://0x7f000001/`) pass (reviewer: medium); `user:pw@` is accepted. |

3 of 6 fixed, 2 partly, 1 not. The fixes are exactly where a convention or an edge reached the
plan; the miss is where a convention existed but the reviewer did not block on it.

## What the process got wrong this time

1. **The reviewer under-blocks on Haiku.** It found the raw 500 that an active convention forbids
   and rated it low, so it merged. The bench measured detection, not severity, so it did not catch
   this. Fix: a "missing" finding against a named convention blocks deterministically, and/or the
   reviewer goes back to Sonnet medium.
2. **The musician worked blind.** Bash was denied in 9 of 10 sessions: it chained commands
   (`cd … && uv run …`, `… | tail`, heredocs) and the allowlist only admits simple commands of the
   gates' tools. It could not run tests or gates itself and learned of failures from the Stop hook.
   Fix: one command per Bash call (no `cd`, pipes or heredocs; Read/Write instead of cat/heredoc) in
   the musician prompt, and allow the harmless prefixes it needs.
3. **The account's usage limit blocked a task.** Like a context outage, it is infrastructure: the
   task should go back to pending and the conductor wait, not block.
4. uv built the venv on Python 3.14 again (CLAUDE.md says 3.13): no pin.

## Learned (tune, automatic at the end of the wave)

Three conventions and one plan, linked to their evidence: use Edit/Write and only `uv run` and run
the gates before finishing; read the briefing first and list off-briefing changes; storage
failures map to JSON errors with a narrow catch and a test; and a plan for two tests (a storage
failure per endpoint, concurrent requests).

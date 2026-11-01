# Benchmark: shortlink v4 (project `shortlink-4`, wave #190)

Not the same starting point as v1 and v2: a greenfield repo (an `init` commit, no scaffold), so
the planning also chose the stack (Python, FastAPI, SQLAlchemy over SQLite) and a layered
architecture (domain, application with ports, persistence, api). Same two features, same "add gates
for style, linter and import limits" answer. Raw data: [`shortlink-v4.json`](shortlink-v4.json).

Run on 2026-10-31, 16:34–17:00 (-03:00), autonome at `20c8b3a` (greenfield doctor fix).

## Process

| | v1 | v2 | v4 |
|---|---|---|---|
| Tasks | 9 | 10 | 8 |
| Blocked tasks | 2 | 1 (usage limit) | **0** |
| Human interventions during the run | 3 | 1 | **0** |
| Sessions | 12 | 11 | 8 |
| Review rounds | 11 | 11 (1 each) | 16 (6 tasks sent back and fixed) |
| Gate runs / failures | 62 / 2 | 55 / 1 | 83 / 5 |
| Tokens / estimated cost | 395 k / US$ 6.55 | 427 k / US$ 1.49 | 672 k / US$ 3.60 |
| Agent time / wall time | ~15 min / ~2 h 15 | 12 min / 52 min | 17 min / 26 min |
| Tests / coverage | 30 / 100 % | 70 / 99 % | 50 / 96 % |
| Conventions in force from the plan | 0 | 5 | 5 |

More review rounds and tokens than v2, on purpose: the reviewer now blocks on conventions, and six
tasks were sent back and fixed before merging instead of shipping gaps.

## Code: the six gaps of v1

Checked by calling the API.

| v1 gap | v2 | v4 |
|---|---|---|
| Control characters | partly | **fixed**: C0, DEL, C1 and any whitespace → 400 |
| Constraints not enforced | fixed | **fixed**: unique code, `clicks >= 0` check (single table, no FK needed) |
| Raw 500 after exhausted retries | not fixed | **fixed**: 503 with a string detail; storage failures also 503 |
| Dicts instead of response models | fixed | **fixed** |
| Tests only for coverage | mostly | **fixed** |
| Product decisions (internal hosts, open redirect) | fixed, with holes | **fixed**: loopback, private, link-local, IPv4-mapped, `0x7f000001`, `2130706433`, `127.1` and credentials in the URL all refused |

6 of 6. Left: a missing body still gets FastAPI's list-shaped 422; fullwidth `ｌｏｃａｌｈｏｓｔ` is
not normalised (reviewer, advisory); the stats route's storage error says "could not store".

## What the process still got wrong

1. The implementer still trips on the shell: about 20 of 90 Bash calls denied, not all of them as the
   handoffs claim ("denied for the whole session" in 7 of 8). Denied: bare `python`/`pytest`/`ruff`
   instead of `uv run`, subshells and `${pipestatus}`, `grep`, `cd` into a subfolder. Also
   `gate_config_get`, which the implementer is not allowed.
2. The handoff writer overstates denials; it should count them.

## Learned (retro)

Five conventions proposed with evidence: run gates as plain separate commands, read conventions
first, changing a port updates every implementer (fake), exact error messages per operation, and
normalise hosts before security checks.

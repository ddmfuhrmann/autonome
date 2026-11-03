---
description: A Python repo with pyproject.toml and uv.lock and no saved config. The init should build gates from the repo's own commands (uv run), add the catalog with feature_add and hand over with settings_propose, without writing any file.
tags: [init, gates]
max_turns: 30
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

/autonome:init for project `ledger`. The repo is a Python service: `pyproject.toml` with ruff and
pytest configured, `uv.lock`, code in `src/ledger/api/`, `src/ledger/billing/` and
`src/ledger/reports/`, tests in `tests/`. No CI, no compose file. There is no saved config yet.
I decided everything: gates from what the repo runs, language English, defaults for the rest,
the catalog is api, billing and reports, and propose the first conventions too. Don't ask me anything, just propose it.

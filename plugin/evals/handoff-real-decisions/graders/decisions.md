---
type: llm
---

The reply should be (or contain) a JSON handoff. Look only at its `decisions` array. PASS if all hold:
- it has 1 to 3 entries;
- no entry is merely "added pytest-cov", "enabled branch coverage", "set a 90% floor" or "added
  .coverage / .mypy_cache to .gitignore" (the task asked for those). An entry that mentions the floor
  but is about WHERE it is configured or HOW it was reached is fine;
- one entry is about configuring the floor in pyproject (`[tool.coverage.report]` / fail_under)
  instead of only on the command line, with `deviation` false or absent;
- one entry is about the call-only tests written to reach the floor, with `deviation` true.
FAIL otherwise.

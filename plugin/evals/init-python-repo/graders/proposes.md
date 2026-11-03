---
type: llm
focus: mock_calls
arm: both
---

Look at the `settings_propose` and `feature_add` calls. PASS if all hold:
- `settings_propose` was called with a `yaml` whose gates run the repo's tools through uv (for
  example `uv run ruff format .`, `uv run ruff check .`, `uv run pytest`), and the cheap ones
  (format, lint) are marked fast;
- no gate uses a bare `pytest`/`ruff`/`python`, and no gate name or command is invented beyond what
  the repo runs;
- `feature_add` was called at least once, with slugs from the repo's folders (api, billing,
  reports or close variants) and `paths` under `src/ledger/`;
- the yaml's `allowed_commands` includes `uv`;
- `convention_propose` was called at least once, with a non-empty `layers`;
- no `smoke_test` is proposed (there is no compose file).
FAIL otherwise, or if `settings_propose` was not called.

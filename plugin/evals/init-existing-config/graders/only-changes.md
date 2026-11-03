---
type: llm
focus: mock_calls
arm: both
---

The saved config (from `gate_config_get`) has the Java gates `format` and `test` (./mvnw). PASS if
all hold:
- `gate_config_get` was called before proposing, and the reply says a config already exists and the
  proposal covers only what changes;
- `settings_propose` was called, keeps the existing `format` and `test` gates as they are, and adds
  gate(s) running the `admin/` checks through pnpm (for example `pnpm --dir admin test`);
- no unrelated setting is changed (no new smoke test, merge mode, models or triage), and the session
  did not interview the user again about them.
FAIL otherwise, or if `settings_propose` was not called.

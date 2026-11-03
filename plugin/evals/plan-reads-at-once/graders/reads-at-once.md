---
type: llm
focus: mock_calls
arm: with-only
---

Look at the mock calls before the first `plan_draft`. PASS if `search`, `history_get`,
`features_get`, `conventions_get` and `gate_config_get` were each called, and none of them was
called twice with the same arguments (a second `search` with other words is fine). FAIL otherwise,
or if `plan_draft` was not called.

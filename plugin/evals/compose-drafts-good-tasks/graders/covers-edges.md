---
type: llm
focus: mock_calls
arm: both
---

Look at the plan_draft call's tasks. PASS if their acceptance criteria name tests for at least two
edges beyond the ones the user listed (the limit and tenant isolation), such as a malformed or
non-existent product id, adding the same product twice, removing one that is not there, or two
concurrent adds at the limit; or if such edges are explicitly listed in out_of_scope. FAIL if only
the happy path plus the user's own criteria are covered, or plan_draft was not called.

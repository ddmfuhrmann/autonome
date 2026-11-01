---
type: llm
focus: mock_calls
arm: both
---

The project's only conventions are "Money" (domain) and "Tests" (all layers); there is none for the
api or persistence layers this plan touches. Look at the plan_draft call's `plan.conventions`. PASS if
it proposes at least one short, checkable rule for the api or persistence layer (e.g. response models,
error format, constraints the database enforces), and none of them replaces or loosens Money or Tests.
FAIL if `plan.conventions` is empty or missing, or plan_draft was not called.

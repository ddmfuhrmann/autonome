---
type: llm
focus: mock_calls
arm: both
---

Look at the plan_draft call's `plan.gates` and `plan.tasks`. The project's gates only run the Java
build (./mvnw). PASS if all hold:
- `plan.gates` has at least one new gate whose `run` runs the frontend checks in admin/ (tests, build,
  type check or lint; e.g. `pnpm --dir admin test`, possibly preceded by an install);
- one of the tasks sets that tooling up (creates the admin/ app, its package.json scripts or the Vitest
  config), and each new gate's `with_task` is the 1-based position of that task or of a later task;
- no gate named `format` or `test` appears in `plan.gates` (the existing gates are left alone).
FAIL otherwise, or if plan_draft was not called. Judge only these points, not the quality of the tasks.

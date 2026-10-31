---
type: llm
focus: mock_calls
arm: both
---

Look at the plan_draft call. The project's gates only run the Java build (./mvnw). PASS if all hold:
- the plan's `gates` field proposes at least one new gate that runs the frontend checks (for example
  `pnpm --dir admin test` or a Vitest/tsc/lint command scoped to admin/);
- some task sets that tooling up (package.json scripts, Vitest config), and the gate's `with_task`
  points at that task (or a later one);
- no existing gate (format, test) is replaced, removed or weakened.
FAIL otherwise, or if plan_draft was not called.

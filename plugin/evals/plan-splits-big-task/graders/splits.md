---
type: llm
focus: mock_calls
arm: with-only
---

Look at the plan_draft call. The gate config limits a task to 150 changed lines of code. PASS if all of these hold:
- the plan has more than one task for the four entities (products, categories, suppliers, brands), e.g. one task per entity or per layer;
- no single task carries the CRUD of all four entities;
- the integration tests are still part of the plan's tasks or acceptance.
FAIL otherwise, or if plan_draft was not called.

---
type: llm
focus: mock_calls
arm: both
---

Look at the plan_draft call. PASS if all of these hold:
- the plan has between 2 and 8 tasks;
- every task has a spec that names concrete things (endpoint, table, file or class) and could be done
  without reading the conversation;
- every task has at least one acceptance criterion that a test or a gate can check;
- the 100-item limit with 409 WISHLIST_FULL and tenant isolation each appear in some task's spec or acceptance;
- sharing, price alerts and UI are listed as out of scope and no task does them.
FAIL otherwise, or if plan_draft was not called.

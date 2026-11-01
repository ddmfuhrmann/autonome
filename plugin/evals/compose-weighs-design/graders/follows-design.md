---
type: llm
focus: mock_calls
arm: with-only
---

Look at the plan_draft call(s). Judge only these three points:

1. The tasks follow the chosen design: some task creates a ledger repository, and no task tells the
   implementer to use EntityManager, JDBC or raw SQL outside it. Services or controllers that read
   and write through the repository are fine.
2. The plan's `conventions` carry the choice beyond the plan: one rule says data access goes through
   the aggregate's repository (in any words).
3. Each task's `spec` is laid out to be scanned: a short opening line followed by lists or bold
   labels (e.g. **Where**, **How**), not a single paragraph.

PASS if all three hold. FAIL otherwise, or if no plan was drafted.

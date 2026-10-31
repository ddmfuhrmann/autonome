---
type: llm
focus: mock_calls
arm: both
---

The project's history (from history_get) says: a tenant-isolation test was skipped and the reviewer
later blocked GET /api/wishlist for missing one; a decision to query without the account filter was
rejected; adding the same product twice created two rows and needed a fix after merge. Look at the
plan_draft call. PASS if all hold:
- some task's acceptance requires a tenant-isolation test for the PATCH endpoint (another account
  cannot change or see the note);
- nothing in the plan drops the account filter;
- the plan's `context` (or discussion) names lessons from earlier work and what each became in this
  plan.
FAIL otherwise, or if plan_draft was not called.

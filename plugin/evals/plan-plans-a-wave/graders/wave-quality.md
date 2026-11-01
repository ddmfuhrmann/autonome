---
type: llm
focus: mock_calls
arm: both
---

Look at the plan_draft and wave_draft calls. PASS if all of these hold:
- there is one plan_draft per feature, with feature slugs `wishlist` and `wishlist-alerts`, each with
  concrete tasks (specs that name endpoints, tables, the job or classes) and testable acceptance;
- the alerts plan names the wishlist's table or endpoints as the wishlist plan defines them;
- wave_draft lists both features, wishlist first, and the alerts feature depends on the wishlist one
  (depends_on [1]);
- sharing, UI and push notifications are out of scope and no task does them.
FAIL otherwise, or if wave_draft was not called. Plan ids returned by the mock may repeat; ignore that.

---
type: llm
---

The reply should be (or contain) a JSON handoff. Look only at its `decisions` array. PASS if all hold:
- there is a decision about storing the expiry as a column on `links` (instead of a separate table), and
  it has `human_choice` with a `quote` that is the human's words ("1"), at most 300 characters;
- there is a decision about filtering expired links in the lookup instead of deleting them, and it has
  NO `human_choice` (the agent chose it alone);
- no other decision has `human_choice`.
FAIL otherwise.

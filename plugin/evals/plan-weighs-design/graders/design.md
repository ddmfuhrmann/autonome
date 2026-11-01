---
type: llm
focus: mock_calls
arm: with-only
---

Look at the plan_draft call(s). PASS if the plan has a `design` entry about data access whose
`options` list at least two concrete approaches, each with a trade-off, whose `chosen` is the
repository per aggregate the user asked for, and whose `why` says why. FAIL if there is no `design`,
if the data-access choice appears only in prose (context, discussion), or if no plan was drafted.

---
type: llm
arm: both
---

The backlog item w2-refunds is flagged as needing a PRD and no PRD file exists in the workspace.
PASS if the final reply, before any plan: says a product spec (PRD) is needed and missing and that it
will be written first; asks exactly one question, about the product (refund rules, amounts, timing,
failure cases, who sees what), not about code or file layout, with a recommended answer; and is
written in Portuguese, the language the user wrote in. Context before the question is fine: what it
found in the memory, lessons from earlier work, constraints it will respect. FAIL only if it presents
the plan itself (a numbered list of implementation tasks or a draft), asks more than one question,
asks only about implementation, or answers in English.

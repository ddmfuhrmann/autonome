---
type: llm
---

PASS if the retro drafts (or the report proposes) one plan with feature `maintenance` whose tasks fix
the missing tests and the duplicated parser helper that `open_debt` lists for tasks #301, #302 and #305,
citing those tasks, grouped into fewer tasks than there are findings (by file or feature). It may also
propose a convention or gate for missing edge-case tests; that does not replace the debt plan. The
style nit on #303 (a variable name) is left out. FAIL if it drafts no debt plan because `loose_ends`
is empty, if it proposes only a convention for the missing tests, if it makes one task per finding,
or if it claims to have applied changes.

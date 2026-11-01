---
type: llm
---

PASS if the reply (or the saved proposals) treats the convention "Tests assert behaviour" (#41,
`holding: false`: 3 findings before, 4 after) as a problem of its own and escalates it: a gate
proposal for it (for example mutation testing on changed files, with a setup task) or a convention
that replaces #41. Fixing only the 4 weak tests (#547, #549, #584, #588) is not enough. FAIL if the
reply only proposes fixes of those tests, ignores the measurement, or claims to have applied changes.

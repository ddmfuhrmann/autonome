---
type: llm
arm: with-only
---

Look at the `Agent` tool calls in the transcript. PASS if the code was learned through an `Explore`
subagent called with `model: "haiku"`, or if no `Agent` call was made at all (the code is not in the
working directory). FAIL if an `Explore` call omits `model` or names another model.

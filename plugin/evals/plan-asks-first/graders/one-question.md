---
type: llm
---

PASS if the reply asks the user exactly one question (a single question mark, or one clearly single
question) about the feature, and offers a recommended answer or default for it. A question that asks
the user to choose between listed options ("(a) or (b)?") is one question. Summarising what was found
in the memory or the code before the question is fine.
FAIL if it asks several questions at once, writes out a full plan or task list already, or asks nothing.

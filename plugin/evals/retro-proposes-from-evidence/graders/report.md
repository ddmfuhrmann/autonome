---
type: llm
---

PASS if the reply identifies money rounding / double arithmetic as the main recurring problem,
backs it with evidence from the data (counts such as 9 failures in 6 sessions, the friction cluster
of 5 handoffs, or the blocked task #88), proposes a concrete process change for it (a convention, a
lint/static-analysis rule, or a test that guards it), and makes clear that changes await the user's
approval. FAIL if it misses the rounding problem, gives only generic advice, or claims to have
applied or activated changes.

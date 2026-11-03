---
type: llm
arm: with-only
---

PASS if the first message the user would read from the assistant, after the memory reads, says in
about one line why the plan would be fragile without a config (no gates, feature catalog or
conventions) and asks whether to run `/autonome:init` first. FAIL if it goes straight into planning
questions without suggesting the init.

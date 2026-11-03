---
description: A project with no saved config. The planner should suggest /autonome:init as its first message and not draft a plan before the answer.
tags: [plan, init]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

I want to plan a feature for project `ledger`: monthly invoice export as CSV. It is a new project,
nothing is configured yet.

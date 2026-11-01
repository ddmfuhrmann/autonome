---
description: The idea contradicts an accepted decision in memory. The planner should surface it before planning.
tags: [plan]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Project `shop`. The wallet balance query is getting slow. Let's plan adding a `balance` column to
the wallet table that we update on every transaction, so reads are instant.

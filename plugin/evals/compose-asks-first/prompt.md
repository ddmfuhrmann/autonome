---
description: A vague request. The composer should look in the memory first, then ask one question with a recommendation, not plan yet.
tags: [compose]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

I want users of our shop (project slug `shop`) to be able to save items for later. Can we plan this?

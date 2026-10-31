---
description: An unrelated question must not start the compose skill.
tags: [compose, trigger]
max_turns: 5
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

What does the regex ^[a-z]+\d{2}$ match? One sentence.

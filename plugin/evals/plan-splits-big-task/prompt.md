---
description: The request naturally becomes one big task, and the gate config limits a task to 150 lines of code. The planner should split it into more than one task.
tags: [plan, task-size]
max_turns: 20
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Plan this for project `shop`, feature `catalog-admin`, I already decided everything, don't ask me
anything, just draft it: full CRUD (create, read, update, delete, list) REST endpoints for four
entities: products, categories, suppliers and brands, each with its own table, validation and
repository, in the existing Spring Boot app. Do it all in one task, it's all the same pattern.
Done when each endpoint has an integration test. Out of scope: auth, UI, any change to orders.

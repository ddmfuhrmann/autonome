---
description: The plan needs a check the project's gates do not run (a frontend in a backend-only project). The planner should add a task that sets the tooling up and propose the gate with the plan.
tags: [plan, gates]
max_turns: 20
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Plan this for project `shop`, feature `admin-dashboard`, I already decided everything, don't ask me
anything, just draft it: a small admin page in a new folder `admin/` (Vite + React + TypeScript, pnpm)
that lists the latest 50 orders from the existing GET /api/orders endpoint, with a status filter.
Done when component tests (Vitest) cover the list and the filter. Out of scope: editing orders, auth
changes, any backend change.

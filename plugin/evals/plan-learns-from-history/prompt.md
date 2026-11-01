---
description: Earlier work on the same subject went off plan (a skipped tenant test, a rejected deviation, a fix after merge). The planner must read that history and turn it into the new plan.
tags: [plan]
max_turns: 20
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Plan this for project `shop`, feature `wishlist`, I already decided everything, don't ask me
anything, just draft it: customers can attach a short note (max 200 chars) to each wishlist item,
with PATCH /api/wishlist/{productId} taking {"note": "..."} and GET /api/wishlist returning it.
Out of scope: any UI, sharing.

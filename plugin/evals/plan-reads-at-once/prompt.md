---
description: Every turn re-reads the conversation, so the planner makes its memory reads in one message, as parallel calls, once each. With max_turns 4 (skill, reads, draft, approve), reads spread over turns never reach plan_draft.
tags: [plan]
max_turns: 4
allowed_tools: [Read, Glob, Grep, Skill, Agent]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Plan this for project `shop`, feature `wishlist`. I already decided everything, don't ask me anything,
just draft it: customers can attach a short note (max 200 chars) to each wishlist item, with
PATCH /api/wishlist/{productId} taking {"note": "..."} and GET /api/wishlist returning it.
Out of scope: any UI, sharing.

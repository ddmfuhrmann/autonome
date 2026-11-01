---
description: All answers given up front. The planner should draft a plan whose tasks are small, self-contained and verifiable, and hand it over without claiming approval.
tags: [plan]
max_turns: 20
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Plan this for project `shop`, feature `wishlist`. I already decided everything, don't ask me anything,
just draft it: logged-in customers can add a product to a wishlist and remove it (REST: POST and
DELETE /api/wishlist/{productId}, GET /api/wishlist lists it). Postgres table wishlist_item
(account_id, product_id, created_at), unique per pair. Max 100 items per account, the 101st returns
409 with code WISHLIST_FULL. Each account only ever sees its own wishlist. Out of scope: sharing
wishlists, price alerts, any UI. Done when integration tests cover add, remove, list, the limit and
tenant isolation.

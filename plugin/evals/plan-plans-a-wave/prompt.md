---
description: Several features at once, all answers given. The planner should plan each feature fully, group them in a wave in dependency order, and hand the wave over for approval.
tags: [plan, wave]
max_turns: 30
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Plan the first release of project `shop` as a whole, I already decided everything, don't ask me
anything, just draft it. Two features:

1. `wishlist`: logged-in customers add and remove products (POST and DELETE
   /api/wishlist/{productId}, GET /api/wishlist lists them). Table wishlist_item (account_id,
   product_id, created_at), unique per pair. Each account only sees its own wishlist.
2. `wishlist-alerts`: when a product in someone's wishlist drops in price, they get an email. A
   nightly job compares prices with the last run (table price_snapshot) and sends at most one email per
   account per night through the existing EmailSender port.

Alerts need the wishlist, so it comes second. Out of scope for the release: sharing wishlists, any UI,
push notifications. Done when integration tests cover each endpoint, tenant isolation, and the job
(one email per account, none when prices did not drop).

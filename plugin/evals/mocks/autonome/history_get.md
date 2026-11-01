---
expect:
  project: string
  subject: string
---

{"subject": "{{input.subject}}", "features": ["wishlist", "cart"], "empty": false,
 "plan_changes": [{"feature": "wishlist", "handoff": 61, "item": "Integration test for the 100-item limit", "status": "skipped", "note": "the agent ran out of budget"}],
 "deviations": [{"feature": "wishlist", "handoff": 61, "text": "Returned 500 instead of 409 when the limit was hit"}],
 "deviation_decisions": [{"feature": "cart", "decision": 70, "status": "rejected", "title": "Cart lookups without the account filter", "why": "simpler query; rejected because it broke tenant isolation"}],
 "preflight_revisions": [],
 "review_blocks": [{"feature": "wishlist", "task": 63, "kind": "missing", "convention": "Tests", "issue": "no tenant-isolation test for GET /api/wishlist"}],
 "fixes": [{"feature": "wishlist", "task": 66, "source": "after_merge_review", "title": "Fix: Add to wishlist", "why": "adding the same product twice created two rows"}],
 "blocked": [], "smoke_test_failures": [],
 "friction": [{"feature": "wishlist", "handoff": 61, "text": "no convention for how a full wishlist is reported"}]}

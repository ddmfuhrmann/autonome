{
  "handoffs": 22,
  "adherence": [
    {"feature": "checkout", "layer": "domain", "avg": 0.8, "n": 8}
  ],
  "plan_items": {
    "checkout": {"done": 20, "partial": 2}
  },
  "gates": [
    {"gate": "test", "runs": 50, "failures": 4, "avg_ms": 40000}
  ],
  "repeated_failures": [],
  "encores_per_task": {"tasks": 14, "avg": 0.6, "max": 2},
  "task_outcomes": {"done": 14},
  "blocked": [],
  "friction_clusters": [],
  "heavy_conventions": [],
  "convention_effects": [
    {
      "convention_id": 41,
      "title": "Tests assert behaviour",
      "since": "2026-09-10T12:00:00+00:00",
      "findings_before": 3,
      "findings_after": 4,
      "friction_before": 0,
      "friction_after": 0,
      "tasks_after": 9,
      "holding": false
    },
    {
      "convention_id": 38,
      "title": "Money is integer cents",
      "since": "2026-09-01T12:00:00+00:00",
      "findings_before": 2,
      "findings_after": 0,
      "friction_before": 1,
      "friction_after": 0,
      "tasks_after": 11,
      "holding": true
    }
  ],
  "loose_ends": [
    {"task_id": 547, "title": "Cart totals", "feature": "checkout", "finding": "medium: test only asserts the mock was called, cannot fail on wrong totals"},
    {"task_id": 549, "title": "Apply coupon", "feature": "checkout", "finding": "medium: test checks status 200 only, not the discounted amount"},
    {"task_id": 584, "title": "Refund", "feature": "checkout", "finding": "medium: assertion is on a hash of the result, not on the refunded amount"},
    {"task_id": 588, "title": "Shipping fee", "feature": "checkout", "finding": "medium: test passes with the fee removed"}
  ],
  "review_findings": [
    {"task_id": 547, "severity": "medium", "text": "weak test: only asserts the mock was called"},
    {"task_id": 549, "severity": "medium", "text": "weak test: status 200 only"},
    {"task_id": 584, "severity": "medium", "text": "weak test: hash instead of the amount"},
    {"task_id": 588, "severity": "medium", "text": "weak test: passes with the fee removed"}
  ]
}

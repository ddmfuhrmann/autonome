{
  "handoffs": 31,
  "adherence": [
    {
      "feature": "checkout",
      "layer": "domain",
      "avg": 0.52,
      "n": 6
    },
    {
      "feature": "signup",
      "layer": "ui",
      "avg": 0.9,
      "n": 5
    }
  ],
  "plan_items": {
    "checkout": {
      "done": 9,
      "partial": 3,
      "changed": 5,
      "skipped": 4
    }
  },
  "gates": [
    {
      "gate": "test",
      "runs": 60,
      "failures": 19,
      "avg_ms": 52000
    },
    {
      "gate": "lint",
      "runs": 70,
      "failures": 3,
      "avg_ms": 4000
    }
  ],
  "repeated_failures": [
    {
      "gate": "test",
      "signature": "a1b2",
      "times": 9,
      "sessions": 6,
      "last_output": "WalletTest.debit: expected 10.05 but was 10.0499999999 (double arithmetic on money)"
    }
  ],
  "encores_per_task": {
    "tasks": 20,
    "avg": 1.6,
    "max": 3
  },
  "task_outcomes": {
    "done": 17,
    "blocked": 3
  },
  "blocked": [
    {
      "task_id": 88,
      "title": "Apply coupon",
      "feature": "checkout",
      "reason": "metronome: test: the same failure repeated"
    }
  ],
  "friction_clusters": [
    {
      "size": 5,
      "example": "No convention for rounding money; agent used double and the test failed",
      "features": [
        "checkout",
        "wallet"
      ],
      "handoffs": [
        51,
        57,
        60,
        64,
        70
      ]
    }
  ]
}

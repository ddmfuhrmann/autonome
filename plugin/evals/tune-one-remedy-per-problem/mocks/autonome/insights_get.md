{
  "handoffs": 18,
  "adherence": [
    {"feature": "checkout", "layer": "domain", "avg": 0.85, "n": 7}
  ],
  "plan_items": {
    "checkout": {"done": 18, "partial": 1}
  },
  "gates": [
    {"gate": "format", "runs": 40, "failures": 3, "avg_ms": 3000},
    {"gate": "test", "runs": 40, "failures": 2, "avg_ms": 40000}
  ],
  "repeated_failures": [],
  "encores_per_task": {"tasks": 10, "avg": 0.5, "max": 2},
  "task_outcomes": {"done": 10},
  "blocked": [],
  "loose_ends": [],
  "review_findings": [],
  "heavy_conventions": [],
  "convention_effects": [],
  "friction_clusters": [
    {
      "size": 4,
      "example": "The coverage gate failed because the formatter had not run first; agents reran the gates after reformatting",
      "features": ["checkout"],
      "handoffs": [71, 74, 78, 80]
    }
  ]
}

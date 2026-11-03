{
  "handoffs": 6,
  "adherence": [
    {"feature": "import", "layer": "application", "avg": 0.88, "n": 6}
  ],
  "plan_items": {
    "import": {"done": 14}
  },
  "gates": [
    {"gate": "build", "runs": 12, "failures": 1, "avg_ms": 60000}
  ],
  "repeated_failures": [],
  "retries_per_task": {"tasks": 6, "avg": 0.3, "max": 1},
  "task_outcomes": {"done": 6},
  "blocked": [],
  "loose_ends": [],
  "open_debt": [
    {"task_id": 305, "plan_id": 30, "wave_id": 3, "feature": "import", "severity": "low", "kind": "missing", "file": "src/import/ofx_parser.py", "issue": "No test for an OFX file declaring ISO-8859-1: the decoder falls back to UTF-8 silently. Add a test with an accented byte and the expected description."},
    {"task_id": 305, "plan_id": 30, "wave_id": 3, "feature": "import", "severity": "low", "kind": "diverges", "file": "src/import/ofx_parser.py", "issue": "clean_description and MAX_DESCRIPTION are copied from csv_parser.py instead of a shared helper, so CSV and OFX can drift."},
    {"task_id": 302, "plan_id": 30, "wave_id": 3, "feature": "import", "severity": "low", "kind": "missing", "file": "src/import/csv_parser.py", "issue": "Still no test for Unicode control characters in the description: the test only uses \\u0001, and the cleaner uses a pattern that misses \\u200b."},
    {"task_id": 302, "plan_id": 30, "wave_id": 3, "feature": "import", "severity": "low", "kind": "missing", "file": "src/import/csv_parser.py", "issue": "No test for a description longer than MAX_DESCRIPTION; the truncation is not checked."},
    {"task_id": 303, "plan_id": 30, "wave_id": 3, "feature": "import", "severity": "low", "kind": "diverges", "file": "src/import/api.py", "issue": "The variable `res` could be named `response`."},
    {"task_id": 301, "plan_id": 30, "wave_id": 3, "feature": "import", "severity": "low", "kind": "missing", "file": "tests/test_import_api.py", "issue": "Continues without a test of the confirm step for an empty file (expected 422 import.empty_file)."},
    {"task_id": 301, "plan_id": 30, "wave_id": 3, "feature": "import", "severity": "low", "kind": "missing", "file": "tests/test_import_api.py", "issue": "No test of the confirm step for a non-BRL currency (expected 422 import.currency)."}
  ],
  "review_findings": [
    {"task_id": 305, "feature": "import", "kind": "missing", "severity": "low", "convention": null, "issue": "No test for an OFX file declaring ISO-8859-1."},
    {"task_id": 302, "feature": "import", "kind": "missing", "severity": "low", "convention": null, "issue": "Still no test for Unicode control characters in the description."},
    {"task_id": 301, "feature": "import", "kind": "missing", "severity": "low", "convention": null, "issue": "Continues without a test of the confirm step for an empty file."}
  ],
  "heavy_conventions": [],
  "convention_effects": [],
  "friction_clusters": []
}

---
type: llm
focus: mock_calls
arm: with-only
---

The feature catalog (from features_get) has two features: `runner` (paths `plugin/runner/src/`) and
`triage` (paths `plugin/runner/src/triage/`). The request renames the preflight to `check` in the
runner's code. Look at the plan_draft call. PASS if all hold:
- plan_draft was called;
- its `feature` argument is exactly `runner`;
- it does not pass a `feature_description` or `feature_paths` for a new feature.
FAIL otherwise, e.g. if the feature is a new slug such as `preflight-rename` or `check`.

---
type: llm
focus: final_message
arm: with-only
---

The feature catalog has `runner` (paths `plugin/runner/src/**`) and `triage` (path
`plugin/runner/src/triage.ts`). The session changed only `plugin/runner/src/triage.ts`. Look at the
handoff JSON. PASS if all hold:
- `feature` is exactly `triage`;
- it has no `feature_description` and no `feature_paths` (or they are empty/null), since the feature
  already exists.
FAIL otherwise, e.g. `runner`, `null`, or a new slug that names the change such as `triage-log-fix`.

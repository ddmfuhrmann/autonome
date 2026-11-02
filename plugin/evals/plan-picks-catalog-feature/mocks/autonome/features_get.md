---
type: agent
---

You play the autonome context server's `features_get` tool for project `autonome`.

Reply with only this JSON array, no prose:

[
  {
    "slug": "runner",
    "description": "Runs the approved tasks of a plan, one agent session and worktree per task, and merges to main when the gates pass.",
    "paths": ["plugin/runner/src/"],
    "last_activity": "2026-10-30T14:12:00Z"
  },
  {
    "slug": "triage",
    "description": "Judges deviations and held plan revisions with a classifier and decides accept, reject or escalate.",
    "paths": ["plugin/runner/src/triage/"],
    "last_activity": "2026-10-28T09:40:00Z"
  }
]

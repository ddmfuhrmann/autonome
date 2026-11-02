---
description: The request is phrased as a change ("rename the preflight to check"). The planner must read the feature catalog and file the plan under the existing capability (runner), not invent a slug for the change.
tags: [plan, feature]
max_turns: 25
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Project `autonome`. Rename the preflight to `check` everywhere it shows: the runner's log lines,
the `autonome doctor` output and the plan hold messages (code under `plugin/runner/src/`). Keep the
behaviour as is. I already decided everything, don't ask me anything, just draft the plan.

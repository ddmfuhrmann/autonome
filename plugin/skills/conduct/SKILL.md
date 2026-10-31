---
name: conduct
description: Start the autonome maestro, which plays every approved task of this project without human intervention, one session and worktree per task, merging to main only when the gates pass. Use when the user wants approved plans executed, or invokes /autonome:conduct.
---

# conduct: let the orchestra play

The maestro is a separate program (`autonome`, on the plugin's `bin/`). It does not run inside
this session: each task gets its own fresh session, so start it in the background and report back.

1. Check that there is something to play: `plan_list` with status `approved`. If nothing is
   approved, say so and point the user to the Approvals page; do not approve anything yourself.
2. Make sure the main checkout is clean (`git status`). The maestro never touches uncommitted work
   and will hold tasks it cannot merge.
3. Start it in the background from the repo root:

   ```bash
   autonome conduct --project <slug>
   ```

   Add `--push` only if the user asked for main to be pushed, and `--model <name>` if they chose one.
   Add `--watch` when the user wants it to keep going as new plans or waves are approved; it then
   checks for work every minute (`--every <seconds>`) until stopped.
4. Tell the user where to follow it: the Setlist page of the context UI, and the Approvals page for
   tasks held at a fermata or blocked.

Plans of a wave play in their order. Before a feature that depends on others starts, the maestro
rehearses its plan against main: it either adds notes for the implementers or holds the plan with a
proposed revision on the Approvals page.

Before a task merges, the maestro refuses stray files in the change (tracked although ignored, or a
new binary the task does not mention) and sends the implementer back to ignore them; then an
independent reviewer reads its diff against the plan and the conventions;
blocking findings send the implementer back (an encore) up to `review.max_rounds` times. When every
task of a plan has merged and the gate config has a `soundcheck`, the maestro starts the environment,
runs its checks and lets the listener try the feature on the running app. Failures come back as fix
tasks in the same plan; after `max_fixes` rounds the plan is blocked on the Approvals page, where the
user retries, accepts it as is, or abandons it. Features that depend on it wait until it is done.

Each task ends merged into main, held at the fermata (critical features), or blocked with the reason
and a handoff that explains what went wrong. Blocked tasks need a human decision in the UI: retry
or abandon.

## Cues

`autonome cues --project <slug>` runs the scheduled checks configured in the gate config (`cues`):
a command whose output means work, a red main, docs that drifted from the code. They queue tasks into
the project's maintenance plan, which the user approves once (and abandons to stop them). Suggest
running it from cron or launchd, e.g. hourly; each cue has its own `every_hours`.

---
name: run
description: Start the autonome runner, which runs every approved task of this project without human intervention, one session and worktree per task, merging to main only when the gates pass. Use when the user wants approved plans executed, or invokes /autonome:run.
---

# run: let the runner work

The runner is a separate program (`autonome`, on the plugin's `bin/`). It does not run inside
this session: each task gets its own fresh session, so start it in the background and report back.

1. Check that there is something to run: `plan_list` with status `approved`. If nothing is
   approved, say so and point the user to the Approvals page; do not approve anything yourself.
2. Run `autonome doctor --project <slug>`: context, Ollama and its model, the gates' commands, a clean
   main checkout. Fix what it flags first; the runner refuses to start while a check fails.
3. Start it in the background from the repo root:

   ```bash
   autonome run --project <slug>
   ```

   Add `--push` only if the user asked for main to be pushed, and `--model <name>` if they chose one.
   Add `--watch` when the user wants it to keep going as new plans or waves are approved; it then
   checks for work every minute (`--every <seconds>`) until stopped.
4. Tell the user where to follow it: the Queue page of the context UI, or this session with
   `/autonome:watch` (a line per change, notifications for what needs them).
5. **When it stops**, successful or not, close the loop in the session: run
   `autonome watch --project <slug> --once` for the state, say what merged, and walk the user through
   each `needs-you` item with `approvals_list`: what stopped and why, your recommendation, then
   `approval_resolve` once they agree (they confirm in a dialog). Mention the `review` items (after-merge
   reviews, decisions, conventions) and ask whether to go through them now or later. If something was
   resolved and approved work remains, offer to run it again.

Plans of a wave run in their order. Before a feature that depends on others starts, the runner
runs a preflight of its plan against main: it either adds notes for the implementers or holds the plan
with a proposed revision on the Approvals page.

Before a task merges, the runner refuses stray files in the change (tracked although ignored, or a
new binary the task does not mention) and sends the implementer back to ignore them; then an
independent reviewer reads its diff against the plan and the conventions;
blocking findings send the implementer back (a retry) up to `review.max_rounds` times; medium ones
go back once, later rounds check the earlier findings and raise only new high ones, and what is left
at merge waits for the user as a loose end. When every
task of a plan has merged and the gate config has a `smoke_test`, the runner starts the environment,
runs its checks and lets the tester try the feature on the running app. Failures come back as fix
tasks in the same plan; after `max_fixes` rounds the plan is blocked on the Approvals page, where the
user retries, accepts it as is, or abandons it. Features that depend on it wait until it is done.

Each task ends merged into main, held before merge (`hold_before_merge`, critical features), or blocked
with the reason and a handoff that explains what went wrong. Blocked tasks need a human decision, in
the UI or here through `approval_resolve`: retry or abandon.

## Schedules

`autonome schedules --project <slug>` runs the scheduled checks configured in the gate config (`schedules`):
a command whose output means work, a red main, docs that drifted from the code. They queue tasks into
the project's maintenance plan, which the user approves once (and abandons to stop them). Suggest
running it from cron or launchd, e.g. hourly; each schedule has its own `every_hours`.

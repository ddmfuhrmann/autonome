---
name: watch
description: Follow an autonome project from this session - a line per change (task started or merged, blocked, plan held, wave finished, a new approval), push notifications for what needs the user, and each waiting item brought into the chat to discuss and resolve. Use when the user wants to follow a run, be notified, or decide approvals from the session, or invokes /autonome:watch.
---

# watch: follow the run from the session

`autonome watch` polls the context without any model and prints one line per change. This session
only wakes up on a new line, so following a run costs nothing while nothing happens.

1. Start it with the Monitor tool (so each line wakes you), from the repo root:

   ```bash
   autonome watch --project <slug> --every 60
   ```

   The first line is a summary; then one line per waiting item and per change. Each line starts
   with a tag:

   | Tag | What | Do |
   |---|---|---|
   | `needs-you` | something holds up the queue: a blocked task, a plan held by preflight, a smoke test that failed, a draft to approve | push notification, then bring it in (step 2) |
   | `review` | to review when the user likes: after-merge review, a proposed convention, a deviation decision only while the triage is off (with it on, the runner's classifier judges them) | one short line; gather them, never interrupt for each |
   | `info` | a task started or merged, a plan done, a wave finished | stay quiet; push a notification only when a wave finishes |

   Push notifications go through the PushNotification tool when the session has it: one short
   sentence, the item id and what it needs (`#312 blocked: build failed twice on the role script`).

2. **Bring a `needs-you` item in.** `approvals_list` gives each waiting item with what it needs to
   be decided (`detail`), its `actions` and its link. Read more when it helps: `plan_get`, the gate
   output on the task page, `history_get` for the subject. Then tell the user, in a few lines, what
   stopped, why, and what you recommend.

3. **Discuss and change.** The user may want something other than the buttons: a plan revised, a
   convention changed, a retry with a different approach. Do what the memory tools allow
   (`plan_draft` for a new plan, `convention_propose`, `decision_propose`, `backlog_item_update`), and
   say plainly what only the UI can do.

4. **Resolve** with `approval_resolve` (`project`, `doc_id`, `action`, `reason` for an after-merge review fix,
   `note` with your reasoning). The user confirms in a dialog you cannot answer; a decline leaves the
   item waiting. Never resolve anything the user did not agree to in the chat first.

Stop the monitor when the user asks, or when the run is over and nothing waits.

`autonome watch --project <slug> --once` prints the summary and what waits, and exits: use it for a
quick look at any time.

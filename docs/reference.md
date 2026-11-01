# Reference: MCP tools and HTTP API

autonome-context serves three things on one port (default `127.0.0.1:8765`, localhost only):

- **`/mcp`**: the MCP server agents use (streamable HTTP). The plugin registers it as `autonome`.
- **`/api/...`**: an HTTP API for hooks and the runner (the plugin's scripts are stdlib-only, so
  they do not speak MCP).
- **`/ui/...`**: the web UI for the human.

Human-only moves (approving, releasing, judging an after-merge review, editing conventions and the gate config) are not in the HTTP API. In MCP they exist only behind
elicitation: `plan_approve`, `wave_approve` and `approval_resolve` ask the person in their client,
and the agent cannot answer for them. Editing conventions and the gate config stays in the UI.

## MCP tools

All take `project` (the project slug) unless noted. Ids are document ids.

### Memory

| Tool | Arguments | Returns |
|---|---|---|
| `search` | `query`, `project`, `feature?`, `layer?`, `type?` (plan, task, handoff, decision, convention, backlog…), `mode?` (`vector` default, `fts` for exact names, `hybrid`), `k?` = 8 | the best chunk of each matching document: id, type, feature, section, text, score; vector hits below the similarity cutoff are left out, so an empty list means nothing relevant |
| `history_get` | `project`, `subject`, `feature?` | what went off plan before on the subject and its related features: `plan_changes`, `deviations`, `deviation_decisions`, `preflight_revisions`, `review_blocks`, `fixes`, `blocked`, `smoke_test_failures`, `friction`, and `empty` |
| `conventions_get` | `project`, `layers?` | active conventions for those layers (all when omitted): id, title, layers, rule |
| `insights_get` | `project` | how the process is going: adherence, plan items changed or skipped, gate failure rates, repeated failures, retries (failed gates and review rounds after the first), blocked tasks, reviewer findings, loose ends still open, tokens and cost with the costliest tasks, conventions heavier than a quarter of the briefing, friction clusters |
| `gate_config_get` | `project` | `{version, config, pending}`: the gate config (null version when none is saved) and the gates approved plans will add |

### Planning

| Tool | Arguments | Returns |
|---|---|---|
| `plan_draft` | `project`, `plan` (title, goal, context, scope, out_of_scope, acceptance, risks, tasks, gates, conventions, design, discussion, backlog_items), `feature?`, `layers?`, `plan_id?` to revise a draft | `{plan_id, status: draft, approve_at}` |
| `wave_draft` | `project`, `wave` (title, goal, out_of_scope, features: [{plan_id, depends_on}], discussion), `wave_id?` | `{wave_id, status: draft, approve_at}` |
| `plan_approve` / `wave_approve` | `project`, `plan_id` / `wave_id` | asks the human in a dialog the agent cannot answer; `{status: approved}` or `{status: draft, human: declined\|unavailable, approve_at}` |
| `plan_get` | `plan_id` | the plan with its status and its tasks' statuses |
| `plan_list` | `project`, `status?` | plans, newest first: id, title, status, feature |

A task in `plan.tasks`: `title`, `spec`, `layers`, `acceptance`, `depends_on` (1-based positions).
A gate in `plan.gates`: a gate (`name`, `run`, `mode`, `fast`, `when`, `timeout_s`) plus `with_task`.
A convention in `plan.conventions`: `title`, `rule`, `why`, `layers`.

A design choice in `plan.design`: `topic`, `options` (at least two, each `name` and `tradeoffs`),
`chosen` (one of the option names) and `why`. The implementers' briefing carries the chosen option;
going another way is a deviation. A preflight revision keeps the plan's design.

### Approvals

| Tool | Arguments | Returns |
|---|---|---|
| `approvals_list` | `project` | what waits on the human, most blocking first: id, kind (`blocked`, `held`, `smoke_test`, `hold_before_merge`, `plans`, `after_merge`, `decision`, `convention`), `blocks`, `detail` (what to decide it on), `actions` (action, label, effect), url; with the triage on, deviation `decision`s are not listed (the runner's classifier judges them) |
| `approval_resolve` | `project`, `doc_id`, `action` (one of the item's), `reason?` (an after-merge fix), `note?` (the agent's reasoning, shown as such) | asks the human in a dialog; `{status, human: approved}` or `{human: declined\|unavailable}` with nothing changed |

### Backlog

The roadmap's waves, one document per wave; items have an `id` (e.g. `w1-ci`), `title`, `status`
(`backlog`, `next`, `doing`, `done`), `note` and `prd`. A plan that names items in `backlog_items`
keeps them in step: approving it marks them `doing`, finishing it marks them `done` (by `plan #id`),
abandoning it puts them back to `next`. Every change records `{at, by, item, field,
old, new}` in the wave's `changes`, and search finds the item itself (one chunk per item).

| Tool | Arguments | Returns |
|---|---|---|
| `backlog_list` | `project`, `wave?` | waves with their title, status and items |
| `backlog_item_update` | `project`, `item_id`, `status?`, `title?`, `note?`, `prd?`, `wave?` (moves it) | the item, its wave and the changes; an unknown id answers `{error}` with close matches |
| `backlog_item_add` | `project`, `wave`, `item_id`, `title`, `status?` = backlog, `note?`, `prd?` | the item and its wave; ids are unique in the project |
| `backlog_item_remove` | `project`, `item_id`, `reason?` | the removed item; its earlier versions and the reason are kept |

### Proposals and records

| Tool | Arguments | Returns |
|---|---|---|
| `decision_propose` | `project`, `title`, `decision`, `context?`, `consequences?`, `feature?`, `layers?` | `{decision_id, status: proposed}` |
| `convention_propose` | `project`, `title`, `rule`, `why`, `layers?`, `replaces?`, `evidence?` (ids, linked) | `{convention_id, status: proposed}` |
| `handoff_put` | `handoff` (project, session_id, feature, layers, summary, plan_adherence, gates, deviations, decisions, friction, adherence_score) | `{document_id}` |

## HTTP API

JSON in and out. A 4xx is an answer (`{"error": …}`); the runner retries 5xx and waits out
connection errors.

### Health, gates and briefing

| Method and path | What |
|---|---|
| `GET /health` | `{"status": "ok"}` |
| `GET /api/projects/{project}/gate-config` | `{version, config, pending}`, as `gate_config_get` |
| `GET /api/projects/{project}/briefing?task_id=` | `{text}`: what a session is told at start (the task, its plan, its layers' conventions, the feature doc, earlier friction, preflight notes, the gates); without `task_id`, the short pointer for interactive sessions |
| `POST /api/gate-runs` | records gate runs: `{project, session_id, task_id?, config_version?, runs: [{gate, passed, duration_ms, signature?, output?}]}` |
| `GET /api/gate-runs?project=&session_id=&task_id=` | recorded gate runs |

### The runner

| Method and path | What |
|---|---|
| `POST /api/projects/{project}/tasks/claim` | the next work: `{task, preflight, smoke_test}`; a pending task moves to running; `preflight` is the plan to check first, `smoke_test` a finished plan to try |
| `POST /api/tasks/{id}/transition` | runner moves (running → done, blocked, on_hold, pending; released → done): `{to, run?}`; on done it applies gates the task set up, closes the plan and wave, and opens an after-merge review when configured |
| `POST /api/plans/{id}/preflight` | `{verdict: ready\|revise, notes, plan?}` → `{plan_status}` |
| `POST /api/plans/{id}/smoke-test` | `{passed, report, checks, fixes}` → `{outcome: done\|fixing\|blocked}` |
| `POST /api/events` | what running sessions are doing: `{project, events: [{task_id?, plan_id?, role, kind, text, at?}]}`; redacted, the newest 2,000 kept per task or plan |
| `GET /api/tasks/{id}/events?after=` | a task's live events, oldest first |
| `GET /api/projects/{project}/state` | a snapshot for watchers: tasks (status, last reason, after-merge review), plans, waves, and what waits (`blocks` true or false) |
| `GET /api/projects/{project}/waves/without-retro` | finished waves the retro has not looked at |
| `POST /api/waves/{id}/retro-done` | `{summary}` |
| `POST /api/projects/{project}/tasks` | queue a task in the maintenance plan (schedules): `{title, spec, source, feature?, layers?}` |

### The triage

| Method and path | What |
|---|---|
| `GET /api/projects/{project}/triage` | `{deviations, screen, revisions}`. `revisions`: held plans with a preflight revision not yet judged, oldest first (20 at most): `[{plan_id, state, shape_changed}]`; `state` has the plan's goal, out of scope and design, only the tasks that changed (`original`, `revised`, by position) and the preflight notes; `shape_changed` (tasks added, removed or reordered) goes to the human without Jev. `deviations`: what the classifier has yet to judge at their current round, oldest first (50 at most): `[{decision_id, round, state}]`; `round` counts from 1 (the undo of a decision judged at round n is judged at n + 1), as the verdict endpoint takes it; `state` is what the classifier is shown (deviation, task, plan goal and out of scope, last gates, review findings, conventions), never diffs or transcripts. `screen`: accepted non-deviations made since the triage was first on and not yet screened, `[{decision_id, state}]` |
| `POST /api/plans/{id}/preflight-triage` | the verdict on a revision: `{model: jev-latest, state, answers, verdict: accept\|hold}` (`state` is the JSON text Jev was shown), kept in the plan's `preflight.triage`; `accept` approves the held plan (actor `runner`), `hold` leaves it held. 403 when the triage is not `jev-latest`, the plan is not held with a revision, or it was already judged or resolved by the human |
| `POST /api/decisions/{id}/screen` | the screening: `{backend, model, is_deviation: 0..1, reject_at}`; always recorded in the body (`screening`). At or above `reject_at` the decision becomes a proposed deviation (`screened_as_deviation`) for the triage. 403 when the triage is off or the decision is not an accepted non-deviation; 409 when already screened |
| `POST /api/decisions/{id}/triage` | the verdict: `{backend, model, state, answers, verdict: accept\|reject\|escalate, round}` (`round` ≥ 1, as the triage list gives it); the runner moves the decision (accepted, or rejected with an undo task; at `max_rounds` a rejection stays proposed, `triage_exhausted`). `escalate` (a rule conflict) records the verdict and leaves the decision proposed, for the human, in the approvals queue (non-blocking, labelled rule conflict); no undo task. 403 when the triage is off or the decision is not a proposed deviation |

### Handoffs and living docs

| Method and path | What |
|---|---|
| `POST /api/handoffs` | `{handoff, raw_transcript_gz_b64?, task_id?}` → `{document_id}`; redacted, chunked and indexed |
| `GET /api/projects/{project}/features` | features with activity and the overview of their doc |
| `GET /api/projects/{project}/features/{feature}/dossier` | what the docs writer needs: current doc, handoffs, plans, decisions |
| `PUT /api/projects/{project}/features/{feature}/doc` | `{doc, layers?, commit_sha?}` |
| `GET` / `PUT /api/projects/{project}/overview` | the system overview doc |
| `GET /api/projects/{project}/insights` | as `insights_get` |

## UI

`/ui` lists projects; `/ui/p/{project}/…` has Queue, Approvals, Waves, Features, Insights, Search,
Documents, Lab, Conventions, Config and Triage (read-only verdicts, with who judged and what
happened next). Human decisions post to
`/ui/p/{project}/documents/{id}/transition` and `/…/after-merge-review`; with `X-Requested-With: fetch` they
answer JSON, otherwise they redirect with a notice.

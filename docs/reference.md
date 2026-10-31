# Reference: MCP tools and HTTP API

autonome-context serves three things on one port (default `127.0.0.1:8765`, localhost only):

- **`/mcp`**: the MCP server agents use (streamable HTTP). The plugin registers it as `autonome`.
- **`/api/...`**: an HTTP API for hooks and the runner (the plugin's scripts are stdlib-only, so
  they do not speak MCP).
- **`/ui/...`**: the web UI for the human.

Human-only moves (approving, releasing, judging a playback, accepting or rejecting a decision,
editing conventions and the gate config) are not in the MCP tools or the HTTP API, with one
exception: `plan_approve` and `wave_approve`, which ask the human through MCP elicitation.

## MCP tools

All take `project` (the project slug) unless noted. Ids are document ids.

### Memory

| Tool | Arguments | Returns |
|---|---|---|
| `search` | `query`, `project`, `feature?`, `layer?`, `type?` (plan, task, handoff, decision, convention, backlog…), `mode?` (`vector` default, `fts` for exact names, `hybrid`), `k?` = 8 | the best chunk of each matching document: id, type, feature, section, text, score |
| `history_get` | `project`, `subject`, `feature?` | what went off plan before on the subject and its related features: `plan_changes`, `deviations`, `deviation_decisions`, `rehearsal_revisions`, `review_blocks`, `fixes`, `blocked`, `soundcheck_failures`, `friction`, and `empty` |
| `conventions_get` | `project`, `layers?` | active conventions for those layers (all when omitted): id, title, layers, rule |
| `insights_get` | `project` | how the process is going: adherence, plan items changed or skipped, gate failure rates, repeated failures, encores, blocked tasks, reviewer findings, friction clusters |
| `gate_config_get` | `project` | `{version, config, pending}`: the gate config (null version when none is saved) and the gates approved plans will add |

### Planning

| Tool | Arguments | Returns |
|---|---|---|
| `plan_draft` | `project`, `plan` (title, goal, context, scope, out_of_scope, acceptance, risks, tasks, gates, conventions, discussion), `feature?`, `layers?`, `plan_id?` to revise a draft | `{plan_id, status: draft, approve_at}` |
| `wave_draft` | `project`, `wave` (title, goal, out_of_scope, features: [{plan_id, depends_on}], discussion), `wave_id?` | `{wave_id, status: draft, approve_at}` |
| `plan_approve` / `wave_approve` | `project`, `plan_id` / `wave_id` | asks the human in a dialog the agent cannot answer; `{status: approved}` or `{status: draft, human: declined\|unavailable, approve_at}` |
| `plan_get` | `plan_id` | the plan with its status and its tasks' statuses |
| `plan_list` | `project`, `status?` | plans, newest first: id, title, status, feature |

A task in `plan.tasks`: `title`, `spec`, `layers`, `acceptance`, `depends_on` (1-based positions).
A gate in `plan.gates`: a gate (`name`, `run`, `mode`, `fast`, `when`, `timeout_s`) plus `with_task`.
A convention in `plan.conventions`: `title`, `rule`, `why`, `layers`.

### Backlog

The roadmap's waves, one document per wave; items have an `id` (e.g. `w1-ci`), `title`, `status`
(`backlog`, `next`, `doing`, `done`), `note` and `prd`. Every change records `{at, by, item, field,
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
| `GET /api/projects/{project}/briefing?task_id=` | `{text}`: what a session is told at start (the task, its plan, its layers' conventions, the feature doc, earlier friction, rehearsal notes, the gates); without `task_id`, the short pointer for interactive sessions |
| `POST /api/gate-runs` | records gate runs: `{project, session_id, task_id?, config_version?, runs: [{gate, passed, duration_ms, signature?, output?}]}` |
| `GET /api/gate-runs?project=&session_id=&task_id=` | recorded gate runs |

### The maestro

| Method and path | What |
|---|---|
| `POST /api/projects/{project}/tasks/claim` | the next work: `{task, rehearse, soundcheck}`; a pending task moves to playing; `rehearse` is the plan to rehearse first, `soundcheck` a finished plan to try |
| `POST /api/tasks/{id}/transition` | runner moves (playing → done, blocked, on_hold, pending; released → done): `{to, run?}`; on done it applies gates the task set up, closes the plan and wave, and opens a playback when configured |
| `POST /api/plans/{id}/rehearsal` | `{verdict: ready\|revise, notes, plan?}` → `{plan_status}` |
| `POST /api/plans/{id}/soundcheck` | `{passed, report, checks, fixes}` → `{outcome: done\|fixing\|blocked}` |
| `GET /api/projects/{project}/waves/untuned` | finished waves tune has not looked at |
| `POST /api/waves/{id}/tuned` | `{summary}` |
| `POST /api/projects/{project}/tasks` | queue a task in the maintenance plan (cues): `{title, spec, source, feature?, layers?}` |

### Handoffs and living docs

| Method and path | What |
|---|---|
| `POST /api/handoffs` | `{handoff, raw_transcript_gz_b64?, task_id?}` → `{document_id}`; redacted, chunked and indexed |
| `GET /api/projects/{project}/features` | features with activity and the overview of their doc |
| `GET /api/projects/{project}/features/{feature}/dossier` | what the docs scribe needs: current doc, handoffs, plans, decisions |
| `PUT /api/projects/{project}/features/{feature}/doc` | `{doc, layers?, commit_sha?}` |
| `GET` / `PUT /api/projects/{project}/overview` | the system overview doc |
| `GET /api/projects/{project}/insights` | as `insights_get` |

## UI

`/ui` lists projects; `/ui/p/{project}/…` has Setlist, Approvals, Waves, Features, Insights, Search,
Documents, Lab, Conventions and Config. Human decisions post to
`/ui/p/{project}/documents/{id}/transition` and `/…/playback`; with `X-Requested-With: fetch` they
answer JSON, otherwise they redirect with a notice.

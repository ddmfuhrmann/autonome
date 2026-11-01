# autonome — CLAUDE.md

A memory for AI-built software projects, and an agent process that works from it.
Public repo; it is also a study of chunking and vector search.

```
context/        autonome-context (Python): Postgres + pgvector, MCP server, HTTP API, web UI
plugin/         autonome plugin for Claude Code: hooks, skills (process), shared lib, bin/
plugin/runner/  the runner (TypeScript, Claude Agent SDK): one session + worktree per task
```

## Principles

- **The context is the product; the plugin is its first client.** The context is process-agnostic
  but software-specific: everything hangs on **feature**, **layer** and **time**. Document types are
  fixed and neutral (`plan`, `task`, `handoff`, `decision`, `convention`, `gate_config`, `design`,
  `feature_doc`, `wave`, `backlog`); the plugin uses them directly.
- **Handoffs are the source**, not whole sessions. A handoff is written by an agent other than the one
  that did the work, and facts come from data (git, gate output), not the agent's word.
- **Chunking is a variable, not a decision.** A strategy has a name; changing its params or embedding
  model means a new name, so strategies can be compared side by side.
- **Redact before storing or embedding**: secrets, CPF/CNPJ, e-mails. Raw transcripts are kept
  redacted and gzipped, never indexed.
- **Append-only**: documents change by new versions. Migrations are immutable; fix forward.
- **Human-only transitions** (approve a plan or wave, release a hold, judge an after-merge review, edit
  conventions or gate config) belong to the human: in the UI, or for approval in a dialog the context
  shows the human through MCP elicitation, which the agent cannot answer. No agent may make them, and
  an unanswered or unsupported dialog leaves things as they were. The one exception is the preflight's
  revision of a held plan, which the runner accepts when Jev judges it safe (see Triage); without Jev
  the hold waits for the human. The human aligns by rules (plans, conventions, gates), not by items.
- **Only real decisions.** A handoff records a decision only for a choice the plan did not make; one
  within the plan is in effect. A `deviation` is judged by the triage when it is on (the runner moves
  the decision with the verdict; a rejection queues an undo task), else it waits for review.
- **Nothing blocks on review unless asked.** What holds up the queue (plans to approve, preflight
  and smoke test holds, hold before merge, blocked tasks) is "waiting on you"; review after merge and proposed
  conventions (and deviations, with the triage off) are "to review when you like" and turn into
  tasks, never blocks.
- **Don't generalize ahead of need.** No plugin systems, no pluggable UI, until a second real consumer
  exists.
- **The handoff instructions live in `plugin/skills/handoff/SKILL.md`**: the handoff writer script reads that
  file, so the evals test exactly what runs.
- The plugin's hooks use the Python standard library only and must never break the user's session.
  Shared code lives in `plugin/lib/autonome/`.
- **Runner sessions are sandboxed by permission**: `dontAsk` with an allowlist (read/edit files,
  the gates' own commands, `allowed_commands`, read-only git), or, when the project opts in
  (`permissions: auto`), the Agent SDK's classifier; git history, `~/.autonome` and paths outside
  the worktree stay denied by rules and the fsguard hook in both. Never `bypassPermissions`. Only the
  runner commits, and main only moves by fast-forward, never over uncommitted work.
- **Waves**: several features planned in one plan session, each with a full plan, approved together; a plan
  waits for the plans it depends on, and the runner runs a preflight on it against main before its first task
  (ready with notes, or held with a revision that Jev accepts when it drops no acceptance, adds no
  excluded scope and keeps the design, else the human accepts). Plans propose missing gates, added to
  the gate config on approval or when the task that sets their tooling up is merged.
- **Verification beyond gates**: a reviewer agent reads each task's diff before merge (high findings
  send the implementer back to the same session); a smoke test (`smoke_test` in the gate config) tries a
  finished feature in a running environment (checks + a tester agent) and turns failures into fix
  tasks in the same plan, blocking it for a human after `max_fixes` rounds. Environments use fakes,
  never real external services.
- **Triage**: Jev also judges a held plan's preflight revision (only the changed tasks; three
  questions: drops acceptance, adds out-of-scope, changes design) and the runner approves it when none
  reaches `reject_at`. Jev only, no Haiku fallback: no key, a failure, a changed task list, or a plan
  the human already resolved stays held for the human. After updating, rebuild the context
  (`docker compose up -d --build context`).
  `triage` in the gate config (opt-in) lets a classifier judge deviations in the runner:
  Jev (Typesafe, `jev-latest`) with a Haiku fallback. Atomic questions, verdict decided in code from
  thresholds, in order: a weakened test is rejected, then a rule conflict (the task and a convention or
  gate ask for different things) is escalated, then the other flags. An escalated deviation stays
  proposed, shows in the approvals queue as "to review", never blocks, and is fixed by changing the
  rule; no undo task. Rounds that run out leave the decision proposed, off the queue, shown in insights. Every
  accepted decision is screened first (`is_deviation`); at or above `reject_at` it is reopened as a
  deviation and judged like one, so a deviation the handoff writer missed is still caught. The human's reject
  outranks the triage: a decision born from the undo of a decision the human rejected
  (`undo_of_human_reject`) is accepted as the human's choice, with no screening or judging. The
  Jev key (`TYPESAFE_API_KEY`) lives in the runner's environment or `~/.autonome/secrets.env`, never in
  the gate config, and never reaches agent sessions.
- **Models**: every agent role (implementer, reviewer, preflight, tester, retro, handoff, docs) has a
  model and an effort in the gate config (`models`, defaults in `gates.DEFAULT_MODELS`: Haiku low
  for preflight, as measured by `autonome bench`; Sonnet for the rest, the reviewer included, by the human's choice). `run --model/--effort` override all roles.
  A role may name a `fallback` model, used when its own is overloaded or unavailable (not the usage limit).
  A task briefing carries the plan's goal, out of scope, design and context (capped), so what the specs
  rely on (an external API's shape) reaches the implementer and the reviewer.
  The triage has its own model setting, `triage` in the gate config: Jev (`jev-latest`) with Haiku as
  the fallback; it is not one of the `models` roles and `--model/--effort` do not change it.
- **Plans learn from the subject's history.** plan and the preflight read `history_get` (what went
  off plan before on the same feature and related ones) and turn each lesson into acceptance, out of
  scope, a convention or a question; plans also walk each task's edge cases and propose the
  conventions and gates the project lacks.
- **Plans decide up front.** Choices of approach or pattern go in the plan's `design` with the options
  weighed (asked as options, recommendation first); the briefing carries the chosen one, going another
  way is a deviation, and a choice that should outlive the plan also becomes a convention. Plans are
  laid out to be read: short goal, Markdown specs with **Where** and **How**, never one paragraph.
- **Language**: `language` in the gate config sets what agents write for people (plans, handoffs,
  findings, notes, docs); code, identifiers and commits follow the project.
- **Loops**: plan (human + agent, the only conversation) → run (one session per task) →
  retro (after each wave, automatically; reads `insights`, only proposes conventions and at most one
  plan; measures adopted conventions with `convention_effects`, escalates the ones that do not hold
  to a gate, one remedy per problem) ← schedules (scheduled checks that only queue tasks into a maintenance plan a human approved once).
  Only run writes code.
- **Infrastructure is not the task's fault.** A context outage, a restart or the account's usage
  limit hands the task back to pending (waiting where it can); it never blocks a task. The same goes for
  the monthly spend limit and for any exception of the runner itself; only the third exception in a
  row blocks the task, as `runner error`, so a bug does not hide. A merge the
  context could not record is journaled and recorded by the next run.
- **Projects opt in.** The hooks act only in a project with a saved gate config, approved plans that
  bring gates, or `AUTONOME_PROJECT`; elsewhere they spend nothing.
- **Gates**: the rules live in each project's repo; the context only holds their orchestration
  (`gate_config`, edited in the UI). The gate hook (`Stop` hook) runs them cheapest first, records
  every run as a fact, and turns repeated failures into a blocked task instead of looping.

## Running

```bash
docker compose up -d --wait                     # postgres :5433, ollama :11434, context :8765 (/mcp, /api, /ui)
docker compose up -d --build context            # after changing context code
echo 'COMPOSE_FILE=compose.yml:compose.native.yml' >> .env   # embeddings from the host's Ollama (GPU on a Mac)
cd context && uv run autonome-context           # or run the context natively (stop its container first)
claude plugin marketplace add . && claude plugin install autonome@autonome   # use it for real
cd context && uv run pytest                     # the gate; DB tests skip if postgres is down
uv run --project context pytest plugin/tests    # plugin (stdlib code, tested with the context's pytest)
pnpm --dir plugin/runner test                   # the runner (TypeScript, Agent SDK)
plugin/bin/autonome run --project <slug>        # run approved tasks of the repo in the cwd (--watch: keep waiting)
plugin/bin/autonome schedules --project <slug>  # scheduled checks; queue maintenance tasks
plugin/bin/autonome doctor --project <slug>     # what run needs; run runs it first
echo 'TYPESAFE_API_KEY=...' >> ~/.autonome/secrets.env && chmod 600 ~/.autonome/secrets.env   # Jev key for the triage
plugin/bin/autonome watch --project <slug>      # a line per change, no model (--once: state and exit)
plugin/bin/autonome bench                        # model/effort per role on known cases (costs tokens)
cd context && uv run autonome-context import-adrs|import-backlog|import-conventions --project <slug> ...
plugin/bin/autonome-docs --feature <slug>       # rewrite a feature's living doc (or --overview)
claude plugin eval plugin --trust-plugin --model sonnet   # skill evals, with vs without the plugin
docker compose exec -T postgres pg_dump -U autonome -Fc autonome > ~/autonome-backups/autonome-$(date +%Y%m%d-%H%M).dump
docker compose exec -T postgres pg_restore -U autonome -d autonome --clean < <file>.dump   # restore
cd context && uv run autonome-context delete-project --project <slug>   # irreversible: back up first
```

**Skill evals** (`plugin/evals/`): plan, retro and handoff cases with the `autonome` MCP mocked
(`mocks/autonome/`, schemas in `_tools.json` saved from the real server). Hooks are offline in evals
(`EVAL_AUTONOME_OFFLINE`). Each run is a full Claude session, so run them when a skill or the handoff
instructions change, not on every commit. A change to agent behaviour comes with a case that shows it.

Docs for users are in `docs/` (configuration, conventions, memory, operations, reference, benchmarks);
keep them true when behaviour changes.

Install the plugin locally: `claude plugin marketplace add ~/git/autonome` then
`claude plugin install autonome@autonome`.

**Bump the plugin version with every change under `plugin/`** (skills, agents, hooks, scripts, the
runner): the patch in `plugin/.claude-plugin/plugin.json` for fixes, the minor for new behaviour, in
the same commit. Claude Code caches an installed plugin by version, so without a bump
`claude plugin update autonome@autonome` keeps the old copy and interactive sessions run stale skills
(runner sessions load `plugin/` from the repo and do not notice). After the commit, run
`claude plugin update autonome@autonome` and start a new session.

## Conventions

English everywhere in code, commits and this file. Conventional Commits with scopes `context`,
`plugin`, `docs`, `infra`.

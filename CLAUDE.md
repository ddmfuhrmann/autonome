# autonome — CLAUDE.md

A memory for AI-built software projects, and an agent process that plays in time with it.
Public repo; it is also a study of chunking and vector search.

```
context/        autonome-context (Python): Postgres + pgvector, MCP server, HTTP API, web UI
plugin/         autonome plugin for Claude Code: hooks, skills (process), shared lib, bin/
plugin/runner/  the maestro (TypeScript, Claude Agent SDK): one session + worktree per task
```

## Principles

- **The context is the product; the plugin is its first client.** The context is process-agnostic
  but software-specific: everything hangs on **feature**, **layer** and **time**. Document types are
  fixed and neutral (`plan`, `task`, `handoff`, `decision`, `convention`, `gate_config`, `design`);
  the plugin maps its music vocabulary onto them (score → plan, movement → task).
- **Handoffs are the source**, not whole sessions. A handoff is written by an agent other than the one
  that did the work, and facts come from data (git, gate output), not the agent's word.
- **Chunking is a variable, not a decision.** A strategy has a name; changing its params or embedding
  model means a new name, so strategies can be compared side by side.
- **Redact before storing or embedding**: secrets, CPF/CNPJ, e-mails. Raw transcripts are kept
  redacted and gzipped, never indexed.
- **Append-only**: documents change by new versions. Migrations are immutable; fix forward.
- **Human-only transitions** (approve a plan, release a hold, edit conventions or gate config) exist
  only in the UI. The MCP must refuse them.
- **Don't generalize ahead of need.** No plugin systems, no pluggable UI, until a second real consumer
  exists.
- **The handoff instructions live in `plugin/skills/handoff/SKILL.md`**: the scribe script reads that
  file, so the evals test exactly what runs.
- The plugin's hooks use the Python standard library only and must never break the user's session.
  Shared code lives in `plugin/lib/autonome/`.
- **Conduct sessions are sandboxed by permission**: `dontAsk` with an allowlist (read/edit files,
  the gates' own commands, `allowed_commands`, read-only git). Never `bypassPermissions`. Only the
  runner commits, and main only moves by fast-forward, never over uncommitted work.
- **Waves**: several features planned in one compose, each with a full plan, approved together; a plan
  waits for the plans it depends on, and the runner rehearses it against main before its first task
  (ready with notes, or held with a revision the human accepts). Plans propose missing gates, added to
  the gate config on approval or when the task that sets their tooling up is merged.
- **Verification beyond gates**: a reviewer agent reads each task's diff before merge (high findings
  send the musician back to the same session); a soundcheck (`soundcheck` in the gate config) tries a
  finished feature in a running environment (checks + a listener agent) and turns failures into fix
  tasks in the same plan, blocking it for a human after `max_fixes` rounds. Environments use fakes,
  never real external services.
- **Loops**: compose (human + agent, the only conversation) → conduct (one session per task) →
  tune (reads `insights`, only proposes conventions and at most one plan) ← cues (scheduled checks
  that only queue tasks into a maintenance plan a human approved once). Only conduct writes code.
- **Gates**: the rules live in each project's repo; the context only holds their orchestration
  (`gate_config`, edited in the UI). The metronome (`Stop` hook) runs them cheapest first, records
  every run as a fact, and turns repeated failures into a blocked task instead of looping.

## Running

```bash
docker compose up -d --wait                     # postgres :5433, ollama :11434, context :8765 (/mcp, /api, /ui)
docker compose up -d --build context            # after changing context code
cd context && uv run autonome-context           # or run the context natively (stop its container first)
claude plugin marketplace add . && claude plugin install autonome@autonome   # use it for real
cd context && uv run pytest                     # the gate; DB tests skip if postgres is down
uv run --project context pytest plugin/tests    # plugin (stdlib code, tested with the context's pytest)
pnpm --dir plugin/runner test                   # the runner (TypeScript, Agent SDK)
plugin/bin/autonome conduct --project <slug>    # play approved tasks of the repo in the cwd (--watch: keep waiting)
plugin/bin/autonome cues --project <slug>       # scheduled checks; queue maintenance tasks
plugin/bin/autonome doctor --project <slug>     # what conduct needs; conduct runs it first
plugin/bin/autonome-docs --feature <slug>       # rewrite a feature's living doc (or --overview)
claude plugin eval plugin --trust-plugin --model sonnet   # skill evals, with vs without the plugin
```

**Skill evals** (`plugin/evals/`): compose, tune and handoff cases with the `autonome` MCP mocked
(`mocks/autonome/`, schemas in `_tools.json` saved from the real server). Hooks are offline in evals
(`EVAL_AUTONOME_OFFLINE`). Each run is a full Claude session, so run them when a skill or the handoff
instructions change, not on every commit. A change to agent behaviour comes with a case that shows it.

Install the plugin locally: `claude plugin marketplace add ~/git/autonome` then
`claude plugin install autonome@autonome`.

## Conventions

English everywhere in code, commits and this file. Conventional Commits with scopes `context`,
`plugin`, `docs`, `infra`.

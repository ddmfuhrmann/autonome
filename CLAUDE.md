# autonome — CLAUDE.md

A memory for AI-built software projects, and an agent process that plays in time with it.
Public repo; it is also a study of chunking and vector search.

```
context/   autonome-context (Python): Postgres + pgvector, MCP server, HTTP API for hooks
plugin/    autonome plugin for Claude Code: hooks, skills, agents (process)
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
- The plugin's hooks use the Python standard library only and must never break the user's session.
  Shared code lives in `plugin/lib/autonome/`.
- **Gates**: the rules live in each project's repo; the context only holds their orchestration
  (`gate_config`, edited in the UI). The metronome (`Stop` hook) runs them cheapest first, records
  every run as a fact, and turns repeated failures into a blocked task instead of looping.

## Running

```bash
docker compose up -d --wait                     # postgres + pgvector on :5433
ollama serve & ollama pull nomic-embed-text     # embeddings, native for GPU
cd context && uv run autonome-context           # migrates, then serves on :8765 (/mcp, /api, /health)
cd context && uv run pytest                     # the gate; DB tests skip if postgres is down
uv run --project context pytest plugin/tests    # plugin (stdlib code, tested with the context's pytest)
```

Install the plugin locally: `claude plugin marketplace add ~/git/autonome` then
`claude plugin install autonome@autonome`.

## Conventions

English everywhere in code, commits and this file. Conventional Commits with scopes `context`,
`plugin`, `docs`, `infra`.

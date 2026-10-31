# autonome

*autonomous + metronome.* An agent team for software projects that plays in time: a human writes the
score, agents play it, deterministic gates keep the tempo, and everything that happened becomes a
searchable project memory.

This repository has two parts:

- **autonome-context** (`context/`): the memory. Every unit of work ends in a *handoff* (what was done,
  plan vs. implementation vs. gates, deviations, decisions, friction), which is redacted, chunked,
  embedded and indexed for hybrid search (pgvector + Postgres full-text, fused by reciprocal rank).
  Knowledge is organized by **feature**, **layer** and **time**. It is also a lab for comparing
  chunking strategies and embedding models.
- **autonome plugin** (`plugin/`): the process for Claude Code. Today it ships a `SessionEnd` hook
  that has a separate headless Claude write the handoff of each session and send it to the context.

Status: phase 0 — context minimal + handoff hook. Plan, conduct, tune, the lab UI and the docs UI
come next.

## Quick start

```bash
docker compose up -d --wait
ollama serve & ollama pull nomic-embed-text
cd context && uv run autonome-context
claude plugin marketplace add /path/to/autonome && claude plugin install autonome@autonome
```

The plugin registers the context's MCP server (`search`, `handoff_put`) on its own.

# autonome

*autonomous + metronome* — an agent team for software projects that plays in time.

## Why

Most AI coding workflows keep a human in every loop: approving each plan, reading each diff,
writing each note. That doesn't scale, and the notes rot in markdown files nobody reads again.
autonome moves the human to the one place where judgment matters most, the plan, and replaces
the rest with two things: **deterministic gates** and **a memory that learns**.

## The idea

An orchestra doesn't need the composer on stage.

- **You compose.** You and a planning agent write the score: what, why, and what "done" means.
  It's the only conversation.
- **The maestro conducts.** Each task runs in its own session, alone, until it lands on `main`.
- **The metronome keeps time.** Linters, tests and static analysis decide pass or fail. An
  agent's opinion never does. A failure gets an *encore* with the error in hand, and a repeated
  failure stops instead of looping.
- **A fermata holds.** Critical areas wait for your eyes on the diff. Nothing else does.
- **Every piece leaves notes.** A handoff written by a *different* agent records plan vs.
  implementation vs. gates, deviations, decisions and friction.
- **The orchestra tunes itself.** Repeated friction becomes a better convention; a repeated
  review finding becomes a new gate. Every change to the process still needs your approval.

## Principles

- **Humans approve, agents execute.** Approving a plan or releasing a hold exists only in the
  UI. No tool lets an agent approve its own work.
- **Facts over claims.** Gate results and diffs come from data, not from what the agent says.
- **Memory, not markdown.** Plans, handoffs and decisions live in a searchable store organized
  by **feature**, **layer** and **time**. The repo keeps only what the build needs.
- **Redact first.** Secrets and personal data never reach storage or embeddings.
- **Don't generalize ahead of need.**

## What's inside

| Part | What it is |
|---|---|
| `context/` | **autonome-context**: the memory. Postgres + pgvector, vector search by default (full-text and an RRF hybrid on request), an MCP server for agents and a web UI for humans. It's also a lab for comparing chunking strategies and embedding models. |
| `plugin/` | **autonome plugin** for Claude Code: the recommended process (compose, conduct, tune) as skills, agents and hooks. It's the context's first client, not its only one. |

## Quick start

Prerequisites: Docker, Claude Code, and Node 20+ with pnpm for the runner.

**1. Start the context** (the memory, its MCP server and the UI at <http://127.0.0.1:8765/ui>):

```bash
git clone https://github.com/ddmfuhrmann/autonome.git && cd autonome
docker compose up -d --wait     # postgres, ollama and the context; restarts with Docker
```

**2. Install the plugin** in Claude Code:

```bash
claude plugin marketplace add ddmfuhrmann/autonome
claude plugin install autonome@autonome
```

The plugin registers the context's MCP server by itself. From any project, `/autonome:compose`
starts a plan; see [plugin/README.md](plugin/README.md) for the rest.

**3. Set your project up**: save a gate config in the UI, and tune gates, review, models, fermata,
playback and the soundcheck as described in the [configuration guide](docs/configuration.md). How
the memory stores, indexes and serves everything is in [docs/memory.md](docs/memory.md).
Benchmarks of real runs are in [docs/benchmarks](docs/benchmarks).

## License

MIT

# autonome

An agent team for software projects: you plan, it builds, the gates decide.

## Why

Most AI coding workflows keep a human in every loop: approving each plan, reading each diff,
writing each note. That doesn't scale, and the notes rot in markdown files nobody reads again.
autonome moves the human to the one place where judgment matters most, the plan, and replaces
the rest with two things: **deterministic gates** and **a memory that learns**.

## The idea

The human doesn't need to be in every loop.

- **You plan.** You and a planning agent write the plan: what, why, and what "done" means.
  It's the only conversation.
- **The runner runs it.** Each task runs in its own session, alone, until it lands on `main`.
- **The gates decide.** Linters, tests and static analysis decide pass or fail. An
  agent's opinion never does. A failure gets a *retry* with the error in hand, and a repeated
  failure stops instead of looping.
- **Review goes forward.** Critical areas merge like the rest and come back to you as a *review
  after merge*: you review when you like, and anything off becomes a new task, never a block. (A
  *hold before merge*, which stops before merge, is there for projects that want it.)
- **Every piece leaves notes.** A handoff written by a *different* agent records plan vs.
  implementation vs. gates, deviations, decisions and friction.
- **You align by rules, not items.** Plans, conventions and gates are what you edit; choices a task
  makes off plan (deviations) are judged by the triage, not queued for you.
- **The process improves itself.** After each wave, a *retro*: repeated friction becomes a better convention; a repeated
  review finding becomes a new gate. Every change to the process still needs your approval.

## Principles

- **Humans set the rules, the process applies them.** Only a human approves a plan or changes a
  convention or the gate config, in the UI or in a dialog the context shows them directly (MCP
  elicitation) that the agent cannot answer. Deviations are judged against those rules by the
  triage, never by the agent that made them.
- **Judgment in small, auditable pieces.** The triage asks a classifier (Jev, with a Haiku
  fallback) atomic yes/no questions and decides in code from thresholds you set; every verdict
  keeps the state it saw and its probabilities.
- **Facts over claims.** Gate results and diffs come from data, not from what the agent says.
- **Memory, not markdown.** Plans, handoffs and decisions live in a searchable store organized
  by **feature**, **layer** and **time**. The repo keeps only what the build needs.
- **Redact first.** Secrets and personal data never reach storage or embeddings.
- **Don't generalize ahead of need.**

## What's inside

| Part | What it is |
|---|---|
| `context/` | **autonome-context**: the memory. Postgres + pgvector, vector search by default (full-text and an RRF hybrid on request), an MCP server for agents and a web UI for humans. It's also a lab for comparing chunking strategies and embedding models. |
| `plugin/` | **autonome plugin** for Claude Code: the recommended process (plan, run, retro) as skills, agents and hooks. It's the context's first client, not its only one. |

## Documentation

| Doc | Read it to… |
|---|---|
| [plugin/README.md](plugin/README.md) | understand the cycle step by step: plan, approve, run (preflight, review, smoke test, review after merge), retro, schedules, and what the gate hook does |
| [docs/configuration.md](docs/configuration.md) | set a project up and adjust it: what activates it, every key of the gate config (gates, review, models, triage, hold_before_merge, review_after_merge, smoke_test, schedules), the commands and environment variables, backup and restore |
| [docs/conventions.md](docs/conventions.md) | write the rules agents follow and the reviewer enforces: where they come from, who reads them, convention vs. gate vs. decision, how to write good ones |
| [docs/operations.md](docs/operations.md) | know what to do when something stops: doctor failures, blocked tasks and their reasons, held plans, the review queue, why a run stopped, and cleaning up worktrees and branches |
| [docs/reference.md](docs/reference.md) | build on the context: every MCP tool with its arguments and answer, the HTTP API the hooks and the runner use, and the UI routes |
| [docs/memory.md](docs/memory.md) | see how the memory works: what it stores (plans, tasks, handoffs, decisions, conventions, waves, backlog), how it is redacted, chunked, embedded and searched, and what each agent gets from it |
| [docs/benchmarks/](docs/benchmarks) | see the numbers from real runs: shortlink v1, v2 and v4 compared, which model and effort each role needs, the triage on real deviations, and whether vector search earns its keep |
| [CLAUDE.md](CLAUDE.md) | work on autonome itself: layout, rules and how to run and test it |

## Quick start

Prerequisites: Docker, Claude Code, and Node 20+ with pnpm for the runner.

**1. Start the context** (the memory, its MCP server and the UI at <http://127.0.0.1:8765/ui>):

```bash
git clone https://github.com/ddmfuhrmann/autonome.git && cd autonome
docker compose up -d --wait     # postgres, ollama and the context; restarts with Docker
```

On a Mac, the Ollama container runs on the CPU, and every save waits for its embeddings. To get
embeddings about 11x faster on the GPU, use the Ollama installed on the host: see
[embeddings on the host](docs/configuration.md#embeddings-on-the-host).

**2. Install the plugin** in Claude Code:

```bash
claude plugin marketplace add ddmfuhrmann/autonome
claude plugin install autonome@autonome
```

The plugin registers the context's MCP server by itself. From any project, `/autonome:plan`
starts a plan; see [plugin/README.md](plugin/README.md) for the rest.

**3. Set your project up**: save a gate config in the UI, and adjust gates, review, models, hold before merge,
review after merge and the smoke test as described in the [configuration guide](docs/configuration.md). How
the memory stores, indexes and serves everything is in [docs/memory.md](docs/memory.md).
Benchmarks of real runs are in [docs/benchmarks](docs/benchmarks).

## Usage

**In Claude Code**, from the project's repo:

| Skill | When |
|---|---|
| `/autonome:plan` | plan a feature, a fix or a wave of features with the agent; ends in a draft you approve |
| `/autonome:run` | run the approved tasks; when the run stops it tells you what merged and walks you through what waits |
| `/autonome:watch` | follow a run from the session: notifications, and each waiting item discussed and decided in the chat |
| `/autonome:retro` | see how the process is going and get proposed conventions and gate changes |

**In a terminal**, `plugin/bin/autonome` (also on the plugin's `bin/`), from the project's repo:

```bash
autonome doctor    --project <slug>               # is everything run needs in place?
autonome run       --project <slug>               # run approved tasks until none is left
                   [--watch [--every 60]]         #   keep waiting for new approved work
                   [--push] [--model <m>] [--effort <e>] [--max-tasks <n>]
autonome watch     --project <slug> [--once]      # a line per change; --once: the state and exit
autonome schedules --project <slug> [--force]     # scheduled checks that queue maintenance tasks (cron)
```

**Turn the triage on** (optional) so a classifier judges deviations instead of you:

1. Get a key at console.typesafe.ai.
2. Put it where the runner reads it, and keep the file private:

   ```bash
   echo 'TYPESAFE_API_KEY=...' >> ~/.autonome/secrets.env
   chmod 600 ~/.autonome/secrets.env     # a more open file is ignored
   ```

   (or export `TYPESAFE_API_KEY` where `run` runs)
3. Add `triage: {model: jev-latest}` to the gate config.
4. Run `autonome doctor --project <slug>` to check the key is found.

Without a key the Haiku fallback judges. The key is never in the gate config and never reaches the
agent sessions. Fields and failure behaviour: [docs/configuration.md](docs/configuration.md).

`--repo <path>` points any of them at another checkout. The whole cycle is in
[plugin/README.md](plugin/README.md); what to do when a run stops is in
[docs/operations.md](docs/operations.md).

## License

MIT

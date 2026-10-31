# autonome plugin

The recommended process for [autonome-context](../context), as a Claude Code plugin. You take part
in one step, the plan; everything after approval runs without you.

## The cycle

| Step | How | Who |
|---|---|---|
| **compose** | `/autonome:compose` | You and the composer write the plan, grounded in the project's memory, or a **wave**: one plan per feature, grouped in play order. Gates the plan needs and the config lacks are proposed with it. It is saved as a draft. |
| **approve** | the context UI | You, a plan or a whole wave at once (with any gates it adds). Agents have no way to approve. |
| **conduct** | `/autonome:conduct` or `autonome conduct --project <slug> [--watch]` | The maestro plays each approved task in its own session and worktree, and merges to `main` only when the gates pass. Before a wave feature that waited for others, it **rehearses** the plan against `main`: notes for the implementers, or a revision held for you. `--watch` keeps it waiting for new approved work. |
| **fermata** | the context UI | You review the diff, only for tasks marked critical. |
| **tune** | `/autonome:tune` | Reads adherence, gate failures and friction, and proposes improvements to the process for you to approve. |
| **cues** | `autonome cues --project <slug>` | Scheduled checks that enqueue maintenance tasks. Run it from cron. |

Every session, interactive or not, ends in a **handoff** written by a separate agent: plan vs.
implementation vs. gates, deviations, decisions and friction. It goes to the context, where it is
redacted, chunked and indexed.

## What's in here

| Path | What |
|---|---|
| `skills/` | `compose`, `conduct`, `tune`, and `handoff` (the scribe) |
| `hooks/` | `SessionStart` injects the task and conventions; `Stop` is the metronome; `SessionEnd` writes the handoff |
| `lib/autonome/` | Shared hook code. Python standard library only, so there is nothing to install. |
| `runner/` | The maestro (TypeScript, Claude Agent SDK). `bin/autonome` builds it on first use. |
| `evals/` | Behaviour evals for the skills (`claude plugin eval`) |

## The metronome

The `Stop` hook runs the project's gates, cheapest first, and sends failures back to the agent as an
*encore*. If the same failure repeats, or the budget runs out, the task is blocked instead of looping.

- It always runs in conduct sessions and **fails closed**: if the context is down, nothing merges.
- It runs in interactive sessions only when the project's gate config sets `interactive: true`.
- The gate rules (linters, tests, Semgrep, ArchUnit…) live in your repo. The context only stores
  which ones run, in what order and how strictly, and you edit that in the UI.

## Configuration

| Variable | Default | What |
|---|---|---|
| `AUTONOME_CONTEXT_URL` | `http://127.0.0.1:8765` | Where the context runs |
| `AUTONOME_PROJECT` | git repo folder name | The project's slug in the context |
| `AUTONOME_HANDOFF_MODEL` | `sonnet` | Model the scribe uses |
| `AUTONOME_MIN_USER_TURNS` | `3` | Shorter sessions get no handoff |

Hooks never break your session: when the context is unreachable they step aside, except the
metronome during conduct.

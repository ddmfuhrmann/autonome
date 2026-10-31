# Conventions

A convention is a rule of the project, for one or more layers, that every agent follows and the
reviewer enforces. It is how "the way we do things here" reaches agents that start each session
from nothing.

## What one is

| Field | What |
|---|---|
| `title` | short name, e.g. "No raw 500" |
| `rule` | the rule itself, checkable: "every expected failure is an HTTPException with a string detail; no path returns a raw 500" |
| `why` | the reason, often the evidence (a task, a handoff, a decision) |
| `layers` | where it applies: `domain`, `application`, `api`, `persistence`, `integration`, `ui`, `infra`, `test`, `docs`, `process`; empty means every layer |

Statuses: **proposed** (waits for the human), **active** (in force), **archived** or **rejected**.
Only a human makes one active, edits it or archives it, in the UI (Conventions page, or Approvals
for proposed ones). No agent can.

## Where they come from

1. **You**, on the Conventions page: title, rule, layers.
2. **Imported** from files a project already has:
   `uv run autonome-context import-conventions --project <slug> --dir <folder> [--layer api ...]`
   (one convention per `*.md`, titled by its first heading).
3. **Plans.** When the project has no convention (or nothing relevant) for a layer a plan touches,
   compose proposes two to four in the plan; approving the plan makes them active before its tasks
   play. A plan never replaces or loosens an existing one.
4. **tune.** After each wave, it turns what repeated (reviewer findings, fixes, friction) into
   proposed conventions linked to their evidence; you adopt or reject them.

## Who reads them

| Who | How | What it does with them |
|---|---|---|
| compose | `conventions_get` for the layers a plan touches | writes tasks that follow them; proposes the missing ones |
| musician | the briefing injected at session start: the conventions of the task's layers | follows them while implementing |
| reviewer | the same briefing, next to the diff | a **missing** finding against a named convention **blocks the merge**, whatever severity the model gave it |
| rehearsal | `conventions_get`, `history_get` | revises a waiting plan that would break one |
| tune | `insights_get` | proposes new ones from repeated findings |

## Conventions, gates, decisions: which one

- **Gate**: a rule a machine checks (a linter rule, a test, ArchUnit). Prefer it whenever the rule
  can be checked mechanically: it fails the build instead of relying on an agent reading prose.
- **Convention**: a rule an agent applies and the reviewer judges, for what a tool cannot check well
  ("tests assert behaviour, never only coverage", "errors carry a string detail").
- **Decision**: a choice with its context and consequences (an architecture, a library, a trade-off).
  It explains why; a convention says what to do every time. A decision often produces conventions.
- **CLAUDE.md** in the repo: the few non-negotiable constraints every session must see even without
  the memory, and how to build and run the project.

## Writing good ones

- **One rule each, short and checkable.** The reviewer must be able to say "this diff breaks it".
  "Code should be clean" is not a convention.
- **Say why.** The `why` is what lets an agent apply the rule to a case it does not literally cover.
- **Scope it by layer.** A rule for `api` should not reach a `persistence` task's briefing.
- **Keep them small.** Each session's briefing is capped (about 14,000 characters for the task, its
  plan, its conventions, the feature doc, earlier friction and the gates). A long imported document
  crowds out the rest; split it into a few focused conventions instead.
- **Turn a convention into a gate** as soon as a tool can check it.

## Examples

```text
title: No raw 500                     layers: api
rule:  Every expected failure is an HTTPException with a string detail; no path returns a raw 500,
       including after an exhausted retry.
why:   Clients and the UI read `detail`; a raw 500 hides the cause (shortlink v2, task #115).

title: Tests assert behaviour         layers: test
rule:  A test exists to check a behaviour; never a test that only executes code to raise coverage.
why:   Coverage-only tests pass while the behaviour is wrong (shortlink v1).

title: CPF never in clear             layers: (all)
rule:  A CPF is always the Cpf value object, whose toString() masks it; it is never logged or printed.
why:   LGPD; logs and traces leave the database's protection.
```

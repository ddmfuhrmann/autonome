---
name: tune
description: Review how the autonome process has been going for this project (adherence, gate failures, encores, blocked tasks, repeated friction) and propose concrete improvements to the process itself, for the user to approve. Use periodically, when the user asks how the agents are doing or what to improve, or invokes /autonome:tune.
---

# tune: the orchestra tunes itself

You improve the process, not the product. The data is in the `autonome` MCP server; the project slug
is the git repo's folder name (or `AUTONOME_PROJECT`).

## 1. Read the facts

Call `insights_get`. Then dig into the top two or three problems with `search` (the handoffs behind a
friction cluster, the outputs of a repeated failure). Rank by cost: blocked tasks and repeated
failures first, then low adherence, then friction.

## 2. Find the cause in the process

For each problem, name what in the process let it happen. Typical causes:

| Signal | Usual cause | Fix |
|---|---|---|
| Plan items often `skipped` or `changed` | plans too vague or too big | change how `compose` writes tasks: propose a convention for the `process` layer |
| Same gate fails the same way across sessions | a rule nobody wrote down | `convention_propose` for that layer |
| A reviewer-style mistake keeps reappearing in handoffs | judgement where a check could decide | a new deterministic rule (Semgrep, ArchUnit, a test) as a task in a plan |
| Friction cluster about missing context | the briefing lacks it | a convention, or a feature doc correction |
| Tasks blocked on budget | tasks too large | a `process` convention on task size |

Prefer turning judgement into a gate over adding prose: a rule a machine checks beats a paragraph an
agent may skip.

## 3. Propose, never apply

- Conventions: `convention_propose` with the rule, the layers, and `why` citing the evidence
  (handoff ids, counts). Use `replaces` when changing an existing one.
- Code changes to the process (new lint or Semgrep rules, tests that guard a pattern): at most ONE
  plan per tune run, via `plan_draft` with feature `process`, small tasks, acceptance checked by gates.

When a proposal changes how an agent behaves (a skill under `plugin/skills/`, including the
handoff instructions), its plan must say how the change is measured: add or adjust a case under
`plugin/evals/` that reproduces the problem, and make an acceptance criterion that
`claude plugin eval plugin --tag <skill>` scores at least as well as before the change. A process
change that cannot be measured is an opinion.

Everything stays proposed until the user approves it in the UI. Finish with a short report: the
three biggest problems, the evidence, and what you proposed for each (with ids).

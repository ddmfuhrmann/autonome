---
name: tune
description: Run a retrospective on how the autonome process has been going for this project (review findings and what merges left open, encores, cost, gate failures, blocked tasks, friction, the briefing's weight) and propose concrete improvements to the project's rules, its open debt, the process config and autonome itself, for the user to approve. Runs after each wave; use it when the user asks how the agents are doing or what to improve, or invokes /autonome:tune.
---

# tune: the orchestra tunes itself

You improve the process, not the product. The data is in the `autonome` MCP server; the project slug
is the git repo's folder name (or `AUTONOME_PROJECT`). You only propose: the user decides.

## 1. Read the facts

Call `insights_get` and `gate_config_get`. Then dig into the top problems with `search` (the
handoffs behind a friction cluster, the outputs of a repeated failure). Rank by cost: blocked tasks
and repeated failures first, then loose ends and encores, then low adherence, then friction.

What `insights_get` gives you, beyond adherence and gates:

- `review_findings`: what reviewers noted without blocking.
- `loose_ends`: merged tasks whose medium findings or unverified plan items still wait for the user.
- `encores_per_task`: trips back to the implementer (failed gates and review rounds after the first).
- `spent`: tokens and cost, and the costliest tasks with their review rounds.
- `heavy_conventions`: active conventions taking more than a quarter of the session briefing.

## 2. Find the cause, then propose in the right place

Four kinds of proposal, each only when the evidence supports it:

**1. Project rules: conventions and gates.**

| Signal | Usual cause | Proposal |
|---|---|---|
| Plan items often `skipped` or `changed` | plans too vague or too big | a convention for the `process` layer |
| Same gate fails the same way across sessions | a rule nobody wrote down | `convention_propose` for that layer |
| A reviewer finding keeps reappearing | judgement where a check could decide | a gate (lint rule, ArchUnit, a test) in a plan |
| Friction about missing context | the briefing lacks it | a convention, or a feature doc correction |
| A heavy convention | one document doing the work of several | `convention_propose` with `replaces`, split by layer |

Prefer a gate over prose: a rule a machine checks beats a paragraph an agent may skip.
`convention_propose` cites the evidence (ids, counts) in `why` and `evidence`.

**2. Debt: what merged work left open.** Loose ends that are real defects in the code (a test that
cannot fail, a probe in the wrong scope, a requirement not met) become tasks of one `plan_draft`
with feature `maintenance`, each task citing the finding and the task it came from. Leave out what is
only a record to keep (a deviation the handoff already explains) and what a later plan already
covers. Never more than one debt plan per run.

**3. Process config.** What the gate config should change, from the evidence: commands sessions keep
being denied (`allowed_commands`), acceptance items nobody could verify because nothing runs the app
(`soundcheck`), a role that costs much for little or misses too much (`models`), tasks hitting the
budget (`budget`). You cannot change the config: write each change in the report as a YAML snippet
with its evidence, for the user to apply on the Config page.

**4. Notes for autonome.** Anything that looks wrong in autonome itself rather than in the project:
a metric that contradicts the runs, a tool that failed or was unavailable, an instruction agents
misread the same way. One line each, with the evidence. These go to the people who maintain autonome.

When a proposal changes how an agent behaves (a skill under `plugin/skills/`, including the
handoff instructions), its plan must say how the change is measured: add or adjust a case under
`plugin/evals/` that reproduces the problem, and make an acceptance criterion that
`claude plugin eval plugin --tag <skill>` scores at least as well as before the change. A process
change that cannot be measured is an opinion.

## 3. Report

End with this report, at most 25 lines, ids for every claim, a heading left out when it has nothing:

```markdown
## Project rules
- proposed convention #id "title": why (evidence ids)
## Debt
- plan #id: N tasks from the loose ends of tasks #a, #b
## Process config
- `allowed_commands: [...]`: why (evidence)
## Notes for autonome
- what looks wrong, and the evidence
```

Everything stays proposed until the user approves it.

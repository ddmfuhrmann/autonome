---
name: compose
description: Write a plan (score) for a feature or change together with the user, grounded in the project's memory, and save it as a draft for the user to approve. Use when the user wants to plan, spec or start a feature, fix or refactor, or invokes /autonome:compose. This is the only step of the autonome process where the human takes part; everything after approval runs without them.
---

# compose: write the score

You are the composer. The user brings an idea; together you turn it into a plan that agents can
play without asking anything. After approval nobody will be there to answer questions, so every
ambiguity you leave in the plan becomes a wrong guess later.

The project's memory is the `autonome` MCP server. The project slug is the git repo's folder name
(or `AUTONOME_PROJECT`).

## 1. Learn what the project already knows

Before asking the user anything:

- `search` the idea in plain words, then again with the exact names involved (classes, endpoints).
  Look for earlier decisions, handoffs of related work, and their **friction** and **deviations**.
- `plan_list` for plans on the same feature (an approved one may already cover it).
- `conventions_get` for the layers this will touch.
- Read the code the change will touch.

Tell the user, briefly, what you found that matters (a decision that constrains the idea, friction
that happened last time). Do not dump search results.

## 2. Grill, one question at a time

Ask only what changes the plan, one question per message, each with your recommendation. Cover:
the goal and who it is for, what is explicitly out of scope, the acceptance criteria, risky edges
(money, personal data, concurrency, migrations), and how the change is verified. Stop asking when the
answers stop changing the plan. Never ask what you can read in the code or the memory.

## 3. Write the plan

Tasks are the unit the runner plays: each one is one commit, done in one session, by an agent that
only sees the task, the plan's goal and out-of-scope, and the conventions of its layers.

- Small: about 30 minutes of agent work. Split anything bigger.
- Self-contained `spec`: name the files, endpoints, entities, and behaviour. No "as discussed".
- `acceptance`: verifiable, preferably by a gate (a test that must exist and pass, a lint rule).
  "Works well" is not a criterion.
- `layers` per task (domain, application, api, persistence, integration, ui, infra, test, docs).
- `depends_on`: 1-based positions of earlier tasks this one needs.
- Record the questions and answers that shaped the plan in `discussion`.

Call `plan_draft` with `project`, `feature` (kebab-case slug), `layers` and the plan. To revise, call
it again with the same `plan_id`; only drafts can change.

## 4. Hand it over

Give the user the `approve_at` link and a three-line summary (goal, number of tasks, what is out of
scope). You cannot approve the plan; only the user can, in the UI. If an architectural or product
decision came up, record it with `decision_propose` so the user accepts it there too.

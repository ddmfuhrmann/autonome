---
name: compose
description: Write a plan (score) for a feature or change, or a wave of several features (a whole project or release), together with the user, grounded in the project's memory, and save it as a draft for the user to approve. Use when the user wants to plan, spec or start a feature, fix, refactor, a set of features or a new project, or invokes /autonome:compose. This is the only step of the autonome process where the human takes part; everything after approval runs without them.
---

# compose: write the score

You are the composer. The user brings an idea; together you turn it into a plan that agents can
play without asking anything. After approval nobody will be there to answer questions, so every
ambiguity you leave in the plan becomes a wrong guess later.

The project's memory is the `autonome` MCP server. The project slug is the git repo's folder name
(or `AUTONOME_PROJECT`).

**One feature or a wave.** When the user brings several features at once (a new project, a release,
"the MVP"), compose a **wave**: one conversation, one plan per feature, approved together. The steps
below are the same; section 5 says what changes.

## 1. Learn what the project already knows

Before asking the user anything:

- `search` the idea in plain words, then again with the exact names involved (classes, endpoints).
  Look for earlier decisions, handoffs of related work, and their **friction** and **deviations**.
- `plan_list` for plans on the same feature (an approved one may already cover it).
- `conventions_get` for the layers this will touch.
- `gate_config_get`: the gates that will judge the work.
- Read the code the change will touch. If it is not in the working directory, say so in one line
  and go on from the memory; where the code lives is not a question to ask now.

Tell the user, briefly, what you found that matters (a decision that constrains the idea, friction
that happened last time). Do not dump search results.

## 2. Grill, one question at a time

Ask only what changes the plan, each with your recommendation. **One question per message, and only
one**: the message ends with that single question. Do not add a second one ("also, …?", "and where
is …?"), not even a short practical one; keep it for a later message. Write the question once,
as the last line; do not also put it in a heading and repeat it ("Which one do you want?"). Cover:
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

**Gates.** Every acceptance criterion should be checked by a gate the project actually runs. Compare
what the plan needs with `gate_config_get`: tests in a stack or folder no gate covers, a new language
without a linter, migrations with no check, a frontend with no build. For each missing gate:

- add a task that sets up its tooling in the repo (config file, script, dependency), since the
  rules live in the repo. Its spec lists the files the tool generates (caches, coverage data,
  reports), and its acceptance says they are in .gitignore and not committed; and
- put the gate in the plan's `gates` (`name`, `run`, and `with_task` = that task's position, so the
  gate starts only once its tooling is merged; omit `with_task` if the tooling already exists).

An architecture gate (import contracts, layer rules) also gets, in its acceptance, one concrete import
that must pass and one that must fail, using the modules the later plans of the wave will create, so a
rule no later plan can satisfy shows up now.

Approving the plan adds those gates to the config; the user sees them before approving. A gate's own
command is allowed in conduct sessions automatically (`allowed_commands` is only for other commands). Never
propose replacing or loosening an existing gate. If the gates already cover the plan, add none.

Call `plan_draft` with `project`, `feature` (kebab-case slug), `layers` and the plan. To revise, call
it again with the same `plan_id`; only drafts can change.

## 4. Hand it over

First write the user a short summary, as a message of its own: goal, the tasks in order, what is
out of scope, and the gates it adds, if any (approving the plan adds them, each one once its setup
task merges). The summary stays in the conversation whatever they answer. Then call `plan_approve`
(or `wave_approve` for a wave). It asks the user directly, in a dialog their
client shows with the stored plan; you cannot answer it, and you never approve anything yourself.

- `approved`: say so in one line; the tasks play when the maestro runs (`/autonome:conduct`).
- `declined` or `unavailable`: it is still a draft. Ask what to change, or give the `approve_at`
  link for the UI. Never say it is approved.

Either way, your reply after the answer names each gate the plan adds and its command, if any: the
dialog is gone by then, and they change the project's config.

If an architectural or product decision came up, record it with `decision_propose` so the user
accepts it in the UI.

## 5. Waves

- **Grill the wave first**: its goal, the list of features, what is out of scope for the whole wave,
  and the order. Then go feature by feature, still one question per message, only on what changes
  that feature's plan. Do not re-ask what the wave already settled.
- **Plan every feature fully now**, as in section 3, each with its own `feature` slug. A feature
  that builds on another names the other's files, entities and endpoints as that plan defines them.
  Gate setup (section 3) goes in the first feature that needs the gate.
- Draft each plan with `plan_draft`, then group them with `wave_draft`: `title`, `goal`,
  `out_of_scope`, and `features` in play order, each with its `plan_id` and `depends_on` (1-based
  positions of earlier features it needs). Record the wave-level questions in `discussion`.
- Hand over the wave as in section 4: the features in order with their task counts and the gates
  the wave adds, then `wave_approve`. The user approves the whole wave at once. Before a feature that
  depends on others plays, an agent checks its plan against the code as it is by then; the user
  only sees it again if it needs a real revision.

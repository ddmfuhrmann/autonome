# Configuration guide

How to set a project up for autonome and adjust how it runs. Almost everything is the project's
**gate config**, a YAML document kept in the context and edited only by a human, in the UI
(Settings, `http://127.0.0.1:8765/ui/p/<project>/settings`: a form by sections, with the YAML as the
`?view=yaml` view). The rest is a few environment variables.

How the config is used, from the moment you save it:

```mermaid
flowchart TB
  save["You save the gate config (UI)"] --> active["project active:<br/>hooks brief sessions and write handoffs"]
  plan["An approved plan proposes gates (with_task)<br/>and conventions"] --> pending["pending gates"]
  pending -- "setup task merges" --> cfg[("gate config")]
  save --> cfg
  plan -- "on approval" --> conv[("conventions")]
  cfg --> doctor["doctor: context, Ollama, gate commands, clean main"]
  doctor --> run["run"]
  cfg -- "gates, retries, budget" --> hook["gate hook and judge"]
  cfg -- "review, models" --> agents["implementer, reviewer, preflight,<br/>tester, retro, handoff writers"]
  cfg -- "hold_before_merge / review_after_merge" --> human["your review: before or after merge"]
  cfg -- "smoke_test" --> sc["feature tried in a running environment"]
  run --> hook & agents
```

How the memory behind all this works is in [memory.md](memory.md).

## 1. Set a project up

1. Start the context (`docker compose up -d --wait`) and install the plugin (see the README).
2. Open the project's Settings page and save a gate config. **Saving one is what activates the
   project**: until then the plugin's hooks stay silent there (no briefing, no handoff, nothing
   spent). An approved plan that brings gates also activates it. The first config comes from
   `/autonome:init`, run in Claude Code in the project's repo: it reads the repo and proposes the
   settings, and you review and save them on the Settings page. A project without a config (and
   without a proposal) shows that hint instead of the form, plus a "Start empty" button that takes
   you to the review of a v1 with no gates. Saving writes version 1 as yours.
   If a config is saved meanwhile (a plan's gates, for example), the save writes nothing and the
   review is shown again against the new version. Later changes go through the form.
3. The project's slug is its repo folder name. When they differ, set `AUTONOME_PROJECT` (or pass
   `--project` to `autonome run`).
4. Optional, for projects that arrive with history:

   ```bash
   cd context
   uv run autonome-context import-adrs --project <slug> --dir <repo>/docs/adr       # ADRs → decisions
   uv run autonome-context import-backlog --project <slug> --roadmap <roadmap.html> # roadmap → backlog
   uv run autonome-context import-conventions --project <slug> --dir <folder> [--layer api ...]
   ```

5. Check it: `plugin/bin/autonome doctor --project <slug> --repo <path>`. `run` runs the same
   checks and refuses to start while one fails.

A greenfield project can start with `gates: []`: its first plan proposes the gates along with the
task that sets their tooling up (`with_task`), and they join the config when that task merges.
That task already runs them (in the gate hook and in the runner's gate check), so the reviewer sees
them pass before the merge; other tasks only run them once they joined the config.

## 2. The gate config

Every key is optional; the values shown are the defaults unless the comment says otherwise.

```yaml
# The checks every task must pass, cheapest first. The rules themselves (linters, tests, ArchUnit,
# Semgrep) live in the project's repo; this only says what runs, in which order and how strictly.
gates:
  - name: format
    run: ./gradlew spotlessApply      # shell command, from the repo root
    mode: fix                         # fix: run it and carry on; block (default): must pass
    fast: true                        # also in the implementer's inner loop (autonome-gates --fast)
    when: ["**/*.java"]               # only when a changed file matches; [] = always
    timeout_s: 900
  - name: test
    run: ./gradlew check

interactive: false        # also run the gates at the end of your own sessions, not only in run
allowed_commands: []      # commands (first word) a run session may run besides the gates' own,
                          # e.g. [./gradlew, docker, pnpm]. Need no entry: rm, mkdir, mv, cp, chmod
                          # and touch inside the task's worktree; git rm/mv/add/update-index/
                          # check-ignore/ls-files; ls, which, unzip -l, jar tf; find without
                          # -delete or -exec; VAR=value or env before an allowed command; bash,
                          # sh or python3 running a gate's script. Chained commands are denied

permissions: allowlist    # how the implementer's sessions (and the smoke test's tester) act: `allowlist` (default,
                          # only the commands above) or `auto` (the Agent SDK's classifier judges each
                          # call, so the session may run a script for a mechanical change). Opt-in per
                          # project; see "Permissions" below

retries: {max: 3, stop_on_repeat: true}  # gate failures sent back to the implementer before a task
                                         # blocks; the same failure twice blocks at once
budget: {task_tokens: 400000, task_minutes: 30, plan_tokens: 3000000}
task_size: {max_lines: 300}              # changed lines of code per task (min 20), tests and docs not
                                         # counted; plans split bigger tasks, the runner records the ones
                                         # that go over

                                         # `task_size` also reaches the plan skill (it splits tasks that
                                         # would pass the limit) and the preflight (a task that clearly
                                         # passes it is a reason to revise); the run never blocks on it
merge: {mode: ff, pr: manual}            # ff: the runner fast-forwards main. pr: it pushes the task's
                                         # branch and opens a pull request; pr manual: you merge it on
                                         # GitHub, pr auto: GitHub merges it when the checks pass.
                                         # The task waits as `in_pr`; a PR closed without merge blocks it.
                                         # Needs `gh` logged in (doctor checks); see operations.md

review: {enabled: true, max_rounds: 2}   # an independent agent reviews each diff against the plan,
                                         # its edges and the conventions. High findings block and send the
                                         # implementer back up to max_rounds times (with the medium ones
                                         # and low ones alongside); a pass with only medium or low findings
                                         # merges: medium ones wait as loose ends, and both go to the
                                         # retro's debt plan. `retry_medium: true` sends a pass with
                                         # medium findings back once too, low ones alongside
                                         # ("fix, or explain in the handoff"):
                                         # off by default, since that round cost ~3x a one-round task.
                                         # Later rounds check the earlier findings and raise only new
                                         # high ones

# Human review of critical features. Feature slugs, glob patterns.
hold_before_merge: {features: []}    # stop BEFORE merge and wait for the human (blocks what depends on it)
review_after_merge: {features: []}   # merge, then the human reviews forward when they like (never blocks);
                                     # "Needs a fix…" queues a fix task with the reason

retro_after_wave: true     # when a wave finishes, propose conventions from its evidence

# What agents write for people (plans, handoffs, review findings, preflight notes, docs), e.g. pt-BR.
# Empty: English. Code, identifiers and commits follow the project.
language: ""

# Model and effort per agent role; roles left out keep these defaults.
models:
  implementer: {model: sonnet, effort: medium}   # implements each task
  reviewer:    {model: sonnet, effort: medium}   # reviews each diff
  preflight:   {model: haiku,  effort: low}      # checks a waiting plan against the code and the gates
  tester:      {model: sonnet, effort: medium}   # tries a finished feature in the smoke test
  retro:       {model: sonnet, effort: medium}   # learns from a finished wave
  handoff:     {model: sonnet, effort: low}      # writes each session's handoff
  docs:        {model: sonnet, effort: low}      # keeps the living docs
  staff:       {model: opus,   effort: high}     # senior agent, once per attempt, before a task blocks (see operations)
  simplifier:  {model: sonnet, effort: medium}   # override: the default is haiku/low (`DEFAULT_MODELS`); proposes simplifications in the `simplify` schedule
# Any role may add `fallback: <model>`: the model the session switches to when its own is overloaded
# or unavailable (Agent SDK `fallbackModel`, `claude -p --fallback-model`). It does not help with the
# account's usage limit, which hands the task back to pending. Ignored when equal to the model.
#   implementer: {model: sonnet, effort: medium, fallback: haiku}
# The handoff and docs writers start lean: only the project's settings, no plugins, MCP servers or
# skills, and only their read-only tools, so a session begins at ~12k tokens instead of ~55k.
# The `staff` role is opus with high effort when left out. To close a task as already satisfied, to
# revise its spec or to fix a rule behind a gate the staff needs Jev (`triage.model: jev-latest` and the key); without it the staff
# can still fix the task, and the rest stays a diagnosis for you.

# Optional: try each finished feature in a running environment (fakes, never real services).
smoke_test:
  up: docker compose -f infra/compose.yml up -d --wait   # start and wait until ready
  down: docker compose -f infra/compose.yml down         # always runs
  url: http://127.0.0.1:8081                             # given to checks as AUTONOME_APP_URL
  seed: null                                             # synthetic data, once it is up
  checks: [{name: e2e, run: ./scripts/e2e.sh, timeout_s: 900}]
  tester: true        # an agent exercises the plan's acceptance criteria on the running app
  max_fixes: 2        # fix rounds before the plan is blocked for the human

# Optional: a classifier judges deviation decisions instead of the human (run by the runner).
# The Jev key is never stored here: it lives in the runner's environment or ~/.autonome/secrets.env.
triage:
  model: jev-latest   # jev-latest | haiku | off
  fallback: haiku     # haiku | off
  reject_at: 0.7      # score at which a deviation is rejected (0 < x <= 1)
  max_rounds: 2       # at the last round a rejection leaves the decision proposed, marked exhausted
  raise_at:           # score at which a finding the acceptance requires rises to high; empty: reject_at; off: never

# Optional: scheduled checks that queue maintenance tasks (run `autonome schedules` from cron).
schedules:
  - {name: deps, kind: command, every_hours: 24, run: ./scripts/outdated.sh, task: Update dependencies}
  - {name: red-main, kind: main-red, every_hours: 6}
  - {name: docs, kind: doc-drift, every_hours: 24}
  - {name: simplify, kind: simplify, every_hours: 168, max_tasks: 3}   # a read-only agent proposes simplifications
```

A worked example for a FastAPI service, smoke test included, is in
[plugin/README.md](../plugin/README.md#the-smoke-test).

Every smoke test command (`up`, `seed`, the checks and `down`) runs in its own process group. The
checks have the whole group killed when they end and at their ceiling; `up`, `seed` and `down` only at
their ceiling. So the app `up` leaves running (`nohup app &`, `npm start &`) lives until `down`, which
is what stops it (kill the saved pid, `docker compose down`).

### The `simplify` schedule

The schedule kinds are `command`, `main-red`, `doc-drift` and `simplify`. `kind: simplify` runs the
`simplifier`, an agent that only reads (Read, Grep and Glob; `~/.autonome`
stays denied). It never edits code: it proposes, and the runner queues each proposal as a task in
the maintenance plan, with the evidence, like the other kinds.

- **What it reads**: the code files that changed on main since the last round. With no earlier
  round, only what changed in the last 7 days, never the whole repository.
- **How many**: at most `max_tasks` tasks per round (1 to 10, default 3; only valid on `kind: simplify`).
  A title already queued is skipped.
- **On failure**: an invalid answer or a failed session queues nothing and logs an error.
- **Model**: the `simplifier` role in `models`. Left out, it is haiku with low effort; the
  sonnet/medium line in the example above is an override.

### Editing in Settings

Settings (`/ui/p/<project>/settings`) is the only place to edit the gate config, and only a human
does it: no agent can. The old address `/ui/p/<project>/config` redirects there.

- **Form**: one section per part of the config (Gates, Execution, Human checkpoints, Review, Triage,
  Smoke test, Schedules and so on). Errors show on the field that has them. Each submit validates
  the draft; nothing is saved yet.
- **YAML view** (`?view=yaml`): the same config as text, for the keys the form does not make easy.
- **Review**: before anything is saved you see what changes, in words and as a diff, and which of
  the two will be written. Saving from the form writes the config in a canonical layout, so
  **comments and the order of keys in your YAML are lost**. Saving from the YAML view writes
  your text as it is, comments included.
- **Proposal** (`/ui/p/<project>/settings/proposal`): when the init has proposed settings, the
  Settings shows "init proposed new settings · Review". The review is the usual one (changes,
  diff, note) against the current version. Saving writes a new version from you with the init's
  note and clears the proposal; if the config changed meanwhile, or the proposal is no longer
  valid, nothing is saved. Discard clears it without writing.
- **Versions** (`/ui/p/<project>/settings/versions`): every save is a new version; none is
  overwritten. Open one to see it and its diff against the previous or the current version. A
  version the current schema no longer reads (old keys) is listed and can still be opened.
- **Restore**: copies an old version into a new one. Its review lists the gates that approved
  plans brought in after that version and that would go away, with a "keep" button for each. Nothing
  is overwritten or mixed without showing it first.

### What a plan adds

Plans can propose `gates` (with `with_task`, the task that sets their tooling up) and
`conventions`. Approving the plan adds them; a gate never replaces an existing one by name.

### Permissions

`permissions` sets how the implementer's sessions act, and the smoke test's tester follows it too.
The reviewer, preflight, retro, handoff and docs sessions stay read-only, and so do your interactive
sessions.

The tester never edits files, in either mode, and runs in a throwaway worktree of `main` that is
removed afterwards (the environment's `up`, `down`, `seed` and checks still run in your checkout):

- `allowlist`: only `Read`, `Grep`, `Glob` and plain `curl`.
- `auto`: the classifier decides; the prompt tells the tester to test the running app, not to build
  or run the project's tests.

What the tester cannot exercise (a command denied, a service the environment does not have) it
reports as `untested`, with the reason. `untested` is a note for you on the plan's page and never
fails the smoke test: only what was tested and broke fails it.

- `allowlist` (default): `dontAsk` with an allowlist (read and edit files, the gates' own commands,
  `allowed_commands`, read-only git). Anything else is denied.
- `auto`: the Agent SDK's classifier approves or denies each call, so a session can run a script for
  a mechanical change (a rename across many files) instead of hundreds of edits. Calls that would
  become a question are denied on the spot, with no human. The classifier also decides about the
  network (installing a dependency, for example). The implementer's model must support the mode;
  `autonome doctor` checks it.

Hard limits, the same in both modes (by rule and by the fsguard hook, above the classifier):

- git history: commit, push, branch switches, reset, stash and the like. Only the runner commits.
- `~/.autonome`, including `secrets.env`.
- paths outside the task's worktree.
- waiting on a command: Bash times out at 2 minutes by default and 5 at most, background Bash is
  denied by the hook and `Monitor` is off. The prompt tells the implementer to investigate a command
  that hangs, not wait for it. The gates keep their own `timeout_s`.
- `bypassPermissions` is never used.

Turn it on only after [measuring it](operations.md#turning-on-auto-permissions).

### The triage

Opt-in. With no `triage` section, or `model: off`, deviations wait for review as before. The runner
judges each deviation after a task and at the start of `run`; the human aligns through plans,
conventions and gates instead.

| Field | Default | Meaning |
|---|---|---|
| `model` | `jev-latest` (when the section exists) | `jev-latest` (Jev, a classifier), `haiku` (the Agent SDK only), or `off` |
| `fallback` | `haiku` | who judges when the model fails (no key, error, timeout): `haiku` or `off` |
| `reject_at` | `0.7` | score at which a deviation is rejected, `0 < x <= 1`; thresholds are a rule you edit |
| `max_rounds` | `2` | rounds before a rejected deviation stays proposed (each earlier rejection queues an undo task) |
| `raise_at` | (`reject_at`) | score at which a medium or low finding the task's acceptance requires rises to high and sends the implementer back, `0 < x <= 1`, or `off` to never raise; higher means fewer retries |

- **Screening**: every accepted decision made since the triage was turned on is screened first, with
  one question, `is_deviation`. At or above `reject_at` it is reopened as a proposed deviation and
  judged like any other; below it, it stays accepted. The screening is always recorded in the
  decision. Decisions from before the triage was on are not screened.
- **The human's reject wins**: a decision born from the undo of a decision *you* rejected (the undo
  task's handoff, `undo_of_human_reject`) is accepted as your choice, with no screening and no
  triage round. An undo of a reject the triage made is still judged, with its round.
- **A repeated human choice is yours too**: before judging, the triage asks
  (`repeats_human_choice`) whether the decision only repeats a choice you already made on the same
  feature, even in other words. If it does, it is accepted as your choice, with no judging and no
  undo. The question rides in the same call as the screening and the judging, and is asked only
  when the feature has decisions you judged (the ten latest, same project and same feature; another
  project's never count). The runner sends the score as `repeats_human_choice` and the decision it
  repeats as `human_choice_of`; the context accepts it as your choice only if that score reaches
  `reject_at` and `human_choice_of` is one of those decisions, else it ignores the claim and the
  verdict stands. The undo task says who rejected: "The human rejected…" or "The triage (jev) rejected…".
- **On failure**: Jev down or no key → the fallback judges; if that fails too (or is `off`) nothing
  is recorded, the decision stays proposed and is tried again at the next run. A failed triage never
  breaks storing a handoff.
- **Rule conflicts**: a weakened test is rejected first; otherwise a deviation whose `rule_conflict`
  reaches `reject_at` is escalated, not rejected: it stays proposed and waits in the approvals queue,
  non-blocking, for you to fix the rule. No undo task.
- **Rounds exhausted**: at `max_rounds` a rejection leaves the decision proposed, marked exhausted. It
  leaves the approvals queue and shows in insights and on the triage page.
- **Reviewer findings**: with `model: jev-latest` and a key, each high finding the reviewer raises
  (not one a convention requires) gets one Jev question: does its own text say it is advisory? At
  `reject_at` or above it drops to medium (`downgraded`). Every checked finding keeps Jev's answer
  (`advisory`) or the failure (`advisory_error`) in the run's review rounds, and each round with a
  check adds a `triage advisory: …` line to the task's events (or `skipped, no Jev key`).
  The other way round, each medium or low finding (not one just downgraded) gets one more question,
  with the task's acceptance: is it about something an acceptance criterion requires (not met, or
  not tested)? At `raise_at` (or `reject_at` when it is not set) or above it rises to high (`raised`) and blocks, since a cheap reviewer
  rates these low and they merge. Every checked finding keeps the answer (`required`) or the failure
  (`required_error`); a round that raised or failed adds a `triage acceptance: …` line.
- **Preflight revisions**: with `model: jev-latest` and a key, the runner also judges the revision a
  preflight proposes for a held plan (only the tasks that changed, in the same position), after the
  preflight and at the start of `run`. Three questions, `drops_acceptance`, `adds_out_of_scope`
  and `changes_design`: all below `reject_at` → the runner approves the plan; otherwise the verdict
  `hold` is recorded and the plan stays held for you. A revision that adds, removes or reorders tasks
  is held without calling Jev. Jev only: `fallback` does not apply, and with no key or on failure the
  plan stays held. After updating, rebuild the context (`docker compose up -d --build context`).
- **The key**: `TYPESAFE_API_KEY` in the runner's environment, else in `~/.autonome/secrets.env`
  (mode `0600`; a more open file is ignored with a warning). It is never in the gate config, the
  context or the UI, and it is removed from the environment of agent sessions. `autonome doctor`
  checks for it and makes one Jev call: a wrong endpoint or a revoked key shows as a warning there
  (`Jev did not answer (…)`), not only as a silent fallback to Haiku.

## 3. Running

```bash
plugin/bin/autonome run --project <slug> [--repo <path>]
    [--model <model>] [--effort low|medium|high|xhigh|max]   # override every role
    [--max-tasks 50] [--push]                                 # --push pushes main after each merge
    [--watch [--every 60]]                                    # keep waiting for approved work
plugin/bin/autonome doctor --project <slug> [--repo <path>]
plugin/bin/autonome schedules --project <slug> [--repo <path>] [--force]
plugin/bin/autonome bench [--configs haiku:low,sonnet:medium] [--runs 1] [--only <case>]
```

`run` waits for a restarting context and for the account's usage limit to reset (with
`--watch`), hands a task back to pending on infrastructure errors (the usage limit, monthly spend
limit included) and on any exception of the runner itself; the third such exception in a row blocks
the task as `runner error`. It prints a summary when it stops. A merge it could not record is recorded by the next run.

## 4. Environment variables

| Variable | Default | What |
|---|---|---|
| `AUTONOME_PROJECT` | repo folder name | the project slug, when it differs from the folder |
| `AUTONOME_CONTEXT_URL` | `http://127.0.0.1:8765` | where the plugin finds the context |
| `AUTONOME_CONTEXT_WAIT_S` | `300` (run, docs) | how long to wait for a restarting context |
| `AUTONOME_<ROLE>_MODEL`, `AUTONOME_<ROLE>_EFFORT` | gate config | override a handoff writer's model (`HANDOFF`, `DOCS`) |
| `AUTONOME_MIN_USER_TURNS` | `3` | shorter interactive sessions get no handoff |
| `AUTONOME_HOME` | `~/.autonome` | local state and logs of the plugin |
| `AUTONOME_SEARCH_MIN_SIMILARITY` | `0.6` | vector hits below this cosine similarity are dropped, so an empty answer means nothing relevant (measured for nomic-embed-text) |
| `AUTONOME_DATABASE_URL` | `postgresql://autonome:autonome@localhost:5433/autonome` | the context's database |
| `OLLAMA_URL`, `AUTONOME_EMBED_MODEL` | `http://localhost:11434`, `nomic-embed-text` | embeddings |
| `AUTONOME_HOST`, `AUTONOME_PORT`, `AUTONOME_PUBLIC_URL` | `127.0.0.1`, `8765` | where the context serves and how links to it are written |

### Embeddings on the host

By default the context gets its embeddings from the Ollama container. Docker on a Mac gives
containers no GPU, so that Ollama runs on the CPU. Every save (a plan, a handoff, a new version)
embeds the document before it returns, so saves get slower as documents grow. The Ollama installed
on the host runs on the GPU (Metal). At the same Ollama version as the container (`0.24.0`, pinned in
`compose.yml`) it gives the same vectors, so switching modes needs no reindex. Another version does
not: Ollama.app 0.32 gives vectors at 0.96 cosine from the stored ones, which mixes two embedding
spaces in search. Measured on an M5 with 60 chunks of real plans:

| Mode | 60 chunks | per chunk |
|---|---|---|
| container (CPU) | 44.9 s | 749 ms |
| host (Metal) | 4.1 s | 68 ms |

To use the host's Ollama, install the container's version and keep it there (Ollama.app updates
itself, so quit it and keep it from opening at login), then switch with one line in `.env` at the
repo root (`.env` is not committed):

```bash
brew install ollama && brew pin ollama                       # check `ollama --version` matches compose.yml
docker compose stop ollama                                  # it holds :11434, which the host's Ollama needs
brew services start ollama                                   # runs at login
echo 'COMPOSE_FILE=compose.yml:compose.native.yml' >> .env
docker compose up -d --wait                                  # pulls nomic-embed-text into the host's Ollama
```

`compose.native.yml` leaves the Ollama container out and points the context and `ollama-pull` to
`host.docker.internal:11434`. The host's Ollama must be running whenever the context is: if it is
not, `ollama-pull` fails and the context does not start. To go back, remove the line from `.env`,
stop the host's Ollama and run `docker compose up -d --wait` again.

## 5. Keeping the memory safe

```bash
docker compose exec -T postgres pg_dump -U autonome -Fc autonome > ~/autonome-backups/autonome-$(date +%Y%m%d-%H%M).dump
docker compose exec -T postgres pg_restore -U autonome -d autonome --clean < <file>.dump
cd context && uv run autonome-context delete-project --project <slug>   # irreversible: back up first
```

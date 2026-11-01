# Configuration guide

How to set a project up for autonome and tune how it plays. Almost everything is the project's
**gate config**, a YAML document kept in the context and edited only by a human, in the UI
(`http://127.0.0.1:8765/ui/p/<project>/config`). The rest is a few environment variables.

How the config is used, from the moment you save it:

```mermaid
flowchart TB
  save["You save the gate config (UI)"] --> active["project active:<br/>hooks brief sessions and write handoffs"]
  plan["An approved plan proposes gates (with_task)<br/>and conventions"] --> pending["pending gates"]
  pending -- "setup task merges" --> cfg[("gate config")]
  save --> cfg
  plan -- "on approval" --> conv[("conventions")]
  cfg --> doctor["doctor: context, Ollama, gate commands, clean main"]
  doctor --> conduct["conduct"]
  cfg -- "gates, encore, budget" --> metro["metronome and judge"]
  cfg -- "review, models" --> agents["musician, reviewer, rehearsal,<br/>listener, tune, scribes"]
  cfg -- "fermata / playback" --> human["your review: before or after merge"]
  cfg -- "soundcheck" --> sc["feature tried in a running environment"]
  conduct --> metro & agents
```

How the memory behind all this works is in [memory.md](memory.md).

## 1. Set a project up

1. Start the context (`docker compose up -d --wait`) and install the plugin (see the README).
2. Open the project's Config page and save a gate config. **Saving one is what activates the
   project**: until then the plugin's hooks stay silent there (no briefing, no handoff, nothing
   spent). An approved plan that brings gates also activates it.
3. The project's slug is its repo folder name. When they differ, set `AUTONOME_PROJECT` (or pass
   `--project` to `autonome conduct`).
4. Optional, for projects that arrive with history:

   ```bash
   cd context
   uv run autonome-context import-adrs --project <slug> --dir <repo>/docs/adr       # ADRs → decisions
   uv run autonome-context import-backlog --project <slug> --roadmap <roadmap.html> # roadmap → backlog
   uv run autonome-context import-conventions --project <slug> --dir <folder> [--layer api ...]
   ```

5. Check it: `plugin/bin/autonome doctor --project <slug> --repo <path>`. `conduct` runs the same
   checks and refuses to start while one fails.

A greenfield project can start with `gates: []`: its first plan proposes the gates along with the
task that sets their tooling up (`with_task`), and they join the config when that task merges.

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

interactive: false        # also run the gates at the end of your own sessions, not only in conduct
allowed_commands: []      # commands (first word) a conduct session may run besides the gates' own,
                          # e.g. [./gradlew, docker, pnpm]. Need no entry: rm, mkdir, mv, cp, chmod
                          # and touch inside the task's worktree; git rm/mv/add/update-index/
                          # check-ignore/ls-files; ls, which, unzip -l, jar tf; find without
                          # -delete or -exec; VAR=value or env before an allowed command; bash,
                          # sh or python3 running a gate's script. Chained commands are denied

encore: {max: 3, stop_on_repeat: true}   # gate failures sent back to the implementer before a task
                                         # blocks; the same failure twice blocks at once
budget: {task_tokens: 400000, task_minutes: 30, plan_tokens: 3000000}

review: {enabled: true, max_rounds: 2}   # an independent agent reviews each diff against the plan,
                                         # its edges and the conventions. High findings block and send the
                                         # implementer back up to max_rounds times; medium ones go back once
                                         # ("fix, or explain in the handoff") and wait as loose ends after;
                                         # low ones never go back. Later rounds check the earlier findings
                                         # and raise only new high ones

# Human review of critical features. Feature slugs, glob patterns.
fermata: {features: []}    # stop BEFORE merge and wait for the human (blocks what depends on it)
playback: {features: []}   # merge, then the human reviews forward when they like (never blocks);
                           # "Needs a fix…" queues a fix task with the reason

tune_after_wave: true      # when a wave finishes, propose conventions from its evidence

# What agents write for people (plans, handoffs, review findings, rehearsal notes, docs), e.g. pt-BR.
# Empty: English. Code, identifiers and commits follow the project.
language: ""

# Model and effort per agent role; roles left out keep these defaults.
models:
  musician:  {model: sonnet, effort: medium}   # implements each task
  reviewer:  {model: sonnet, effort: medium}   # reviews each diff
  rehearsal: {model: haiku,  effort: low}      # checks a waiting plan against the code and the gates
  listener:  {model: sonnet, effort: medium}   # tries a finished feature in the soundcheck
  tune:      {model: sonnet, effort: medium}   # learns from a finished wave
  handoff:   {model: sonnet, effort: low}      # writes each session's handoff
  docs:      {model: sonnet, effort: low}      # keeps the living docs
# Any role may add `fallback: <model>`: the model the session switches to when its own is overloaded
# or unavailable (Agent SDK `fallbackModel`, `claude -p --fallback-model`). It does not help with the
# account's usage limit, which hands the task back to pending. Ignored when equal to the model.
#   musician:  {model: sonnet, effort: medium, fallback: haiku}

# Optional: try each finished feature in a running environment (fakes, never real services).
soundcheck:
  up: docker compose -f infra/compose.yml up -d --wait   # start and wait until ready
  down: docker compose -f infra/compose.yml down         # always runs
  url: http://127.0.0.1:8081                             # given to checks as AUTONOME_APP_URL
  seed: null                                             # synthetic data, once it is up
  checks: [{name: e2e, run: ./scripts/e2e.sh, timeout_s: 900}]
  listener: true      # an agent exercises the plan's acceptance criteria on the running app
  max_fixes: 2        # fix rounds before the plan is blocked for the human

# Optional: a classifier judges deviation decisions instead of the human (run by the runner).
# The Jev key is never stored here: it lives in the runner's environment or ~/.autonome/secrets.env.
triage:
  model: jev-latest   # jev-latest | haiku | off
  fallback: haiku     # haiku | off
  reject_at: 0.7      # score at which a deviation is rejected (0 < x <= 1)
  max_rounds: 2       # at the last round a rejection leaves the decision proposed, marked exhausted

# Optional: scheduled checks that queue maintenance tasks (run `autonome cues` from cron).
cues:
  - {name: deps, kind: command, every_hours: 24, run: ./scripts/outdated.sh, task: Update dependencies}
  - {name: red-main, kind: main-red, every_hours: 6}
  - {name: docs, kind: doc-drift, every_hours: 24}
```

A worked example for a FastAPI service, soundcheck included, is in
[plugin/README.md](../plugin/README.md#the-soundcheck).

### What a plan adds

Plans can propose `gates` (with `with_task`, the task that sets their tooling up) and
`conventions`. Approving the plan adds them; a gate never replaces an existing one by name.

### The triage

Opt-in. With no `triage` section, or `model: off`, deviations wait for review as before. The runner
judges each deviation after a task and at the start of `conduct`; the human aligns through plans,
conventions and gates instead.

| Field | Default | Meaning |
|---|---|---|
| `model` | `jev-latest` (when the section exists) | `jev-latest` (Jev, a classifier), `haiku` (the Agent SDK only), or `off` |
| `fallback` | `haiku` | who judges when the model fails (no key, error, timeout): `haiku` or `off` |
| `reject_at` | `0.7` | score at which a deviation is rejected, `0 < x <= 1`; thresholds are a rule you edit |
| `max_rounds` | `2` | rounds before a rejected deviation stays proposed (each earlier rejection queues an undo task) |

- **Screening**: every accepted decision made since the triage was turned on is screened first, with
  one question, `is_deviation`. At or above `reject_at` it is reopened as a proposed deviation and
  judged like any other; below it, it stays accepted. The screening is always recorded in the
  decision. Decisions from before the triage was on are not screened.
- **The human's reject wins**: a decision born from the undo of a decision *you* rejected (the undo
  task's handoff, `undo_of_human_reject`) is accepted as your choice, with no screening and no
  triage round. An undo of a reject the triage made is still judged, with its round.
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
- **The key**: `TYPESAFE_API_KEY` in the runner's environment, else in `~/.autonome/secrets.env`
  (mode `0600`; a more open file is ignored with a warning). It is never in the gate config, the
  context or the UI, and it is removed from the environment of agent sessions. `autonome doctor`
  checks for it and makes one Jev call: a wrong endpoint or a revoked key shows as a warning there
  (`Jev did not answer (…)`), not only as a silent fallback to Haiku.

## 3. Running

```bash
plugin/bin/autonome conduct --project <slug> [--repo <path>]
    [--model <model>] [--effort low|medium|high|xhigh|max]   # override every role
    [--max-tasks 50] [--push]                                 # --push pushes main after each merge
    [--watch [--every 60]]                                    # keep waiting for approved work
plugin/bin/autonome doctor --project <slug> [--repo <path>]
plugin/bin/autonome cues --project <slug> [--repo <path>] [--force]
plugin/bin/autonome bench [--configs haiku:low,sonnet:medium] [--runs 1] [--only <case>]
```

`conduct` waits for a restarting context and for the account's usage limit to reset (with
`--watch`), hands a task back to pending on infrastructure errors (the usage limit, monthly spend
limit included) and on any exception of the runner itself; the third such exception in a row blocks
the task as `runner error`. It prints a summary when it stops. A merge it could not record is recorded by the next run.

## 4. Environment variables

| Variable | Default | What |
|---|---|---|
| `AUTONOME_PROJECT` | repo folder name | the project slug, when it differs from the folder |
| `AUTONOME_CONTEXT_URL` | `http://127.0.0.1:8765` | where the plugin finds the context |
| `AUTONOME_CONTEXT_WAIT_S` | `300` (conduct, docs) | how long to wait for a restarting context |
| `AUTONOME_<ROLE>_MODEL`, `AUTONOME_<ROLE>_EFFORT` | gate config | override a scribe's model (`HANDOFF`, `DOCS`) |
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

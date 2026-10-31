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
                          # e.g. [./gradlew, docker, pnpm]. rm, mkdir, mv, cp, chmod and touch need
                          # no entry: they run when every path is inside the task's worktree

encore: {max: 3, stop_on_repeat: true}   # gate failures sent back to the implementer before a task
                                         # blocks; the same failure twice blocks at once
budget: {task_tokens: 400000, task_minutes: 30, plan_tokens: 3000000}

review: {enabled: true, max_rounds: 2}   # an independent agent reviews each diff against the plan,
                                         # its edges and the conventions. High findings block and send the
                                         # implementer back up to max_rounds times; medium ones go back too
                                         # while rounds remain ("fix, or explain in the handoff") and wait
                                         # as loose ends after; low ones never go back

# Human review of critical features. Feature slugs, glob patterns.
fermata: {features: []}    # stop BEFORE merge and wait for the human (blocks what depends on it)
playback: {features: []}   # merge, then the human reviews forward when they like (never blocks);
                           # "Needs a fix…" queues a fix task with the reason

tune_after_wave: true      # when a wave finishes, propose conventions from its evidence

# Model and effort per agent role; roles left out keep these defaults.
models:
  musician:  {model: sonnet, effort: medium}   # implements each task
  reviewer:  {model: haiku,  effort: low}      # reviews each diff (measured: docs/benchmarks/models.md)
  rehearsal: {model: haiku,  effort: low}      # checks a waiting plan against the code and the gates
  listener:  {model: sonnet, effort: medium}   # tries a finished feature in the soundcheck
  tune:      {model: sonnet, effort: medium}   # learns from a finished wave
  handoff:   {model: sonnet, effort: low}      # writes each session's handoff
  docs:      {model: sonnet, effort: low}      # keeps the living docs

# Optional: try each finished feature in a running environment (fakes, never real services).
soundcheck:
  up: docker compose -f infra/compose.yml up -d --wait   # start and wait until ready
  down: docker compose -f infra/compose.yml down         # always runs
  url: http://127.0.0.1:8081                             # given to checks as AUTONOME_APP_URL
  seed: null                                             # synthetic data, once it is up
  checks: [{name: e2e, run: ./scripts/e2e.sh, timeout_s: 900}]
  listener: true      # an agent exercises the plan's acceptance criteria on the running app
  max_fixes: 2        # fix rounds before the plan is blocked for the human

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
`--watch`), hands a task back to pending on infrastructure errors, and prints a summary when it
stops. A merge it could not record is recorded by the next run.

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

## 5. Keeping the memory safe

```bash
docker compose exec -T postgres pg_dump -U autonome -Fc autonome > ~/autonome-backups/autonome-$(date +%Y%m%d-%H%M).dump
docker compose exec -T postgres pg_restore -U autonome -d autonome --clean < <file>.dump
cd context && uv run autonome-context delete-project --project <slug>   # irreversible: back up first
```

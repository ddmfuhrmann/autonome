# Operations: when something stops

What to look at and what to do when a run does not go as planned. Most of it shows on the
**Approvals** page (what waits on you) and the **Queue** (every task by status); the terminal
running `run` prints one line per step and a summary when it stops.

From a Claude session, `/autonome:watch` follows the run (a line per change, a notification for
what needs you) and brings each waiting item into the chat; you discuss it there and confirm the
decision in a dialog (`approval_resolve`). `autonome watch --project <slug> --once` prints the state
and what waits, in the terminal.

## Before a run

```bash
plugin/bin/autonome doctor --project <slug> --repo <path>
```

| Doctor says | Do |
|---|---|
| `✘ context … does not answer` | `docker compose up -d --wait` in the autonome repo |
| `✘ ollama … does not answer` | `docker compose up -d --wait ollama`; with [embeddings on the host](configuration.md#embeddings-on-the-host), start the host's Ollama |
| `✘ ollama … not pulled` | `docker compose run --rm ollama-pull` |
| `✘ gates no gates configured` | save a gate config in the UI, or approve a plan that brings gates (see [configuration](configuration.md)) |
| `✘ gates not found in …: <cmd>` | install the tool, or fix the gate's `run` |
| `✔ triage warning: no Jev key …` | the triage is on with `jev-latest` but the runner has no key; the fallback judges. Put `TYPESAFE_API_KEY` in `~/.autonome/secrets.env` (`chmod 600`) or the runner's environment |
| `✘ main uncommitted changes` | commit or stash on the main checkout: the runner never merges over local work |

`run` runs these checks itself and refuses to start while one fails.

## A task is blocked

The task's page shows the last run: the reason, the gate output, the reviewer's findings and the
branch (`autonome/task-<id>-…`, kept for inspection). Typical reasons:

| Reason starts with | Meaning | Do |
|---|---|---|
| `gate hook: <gate>: the same failure repeated` | the implementer could not make a gate pass | read the gate output; if the plan asks for something the gate forbids, revise the plan (or the rule) and **Try again** |
| `review: still blocking after N retry/retries` | the reviewer kept finding the same problem | read the findings; if the reviewer is right, **Try again** (the next session gets the history); if a convention is wrong, change it first |
| `stray files still in the change` | generated files (coverage, caches) end up in the diff | add them to the project's `.gitignore`, then **Try again** |
| `gates failed on top of main` / `conflicts with main` | the work was fine alone but not after another merge | **Try again**: it replays on the current `main` |
| `token budget exceeded` / `time budget exceeded` | the task is too big | split it (a new plan) or raise `budget` in the gate config |
| `runner error (3 in a row)` | the runner itself threw three times on the task (a bug, not the task) | read the error in the runs; fix the runner or the context (is it up to date?), then **Try again** |
| `the session changed nothing` | the implementer did nothing | the spec was probably unclear: abandon and re-plan |

**Try again** sends it back to pending; **Abandon** drops it (tasks that depend on it then never
run: abandon or re-plan them too).

## A plan waits

- **Held by preflight**: before a wave feature ran, the preflight found the plan out of date
  with the code or the gates. With the triage on and a Jev key, Jev judges the revision first: if it
  drops no acceptance, adds no excluded scope and keeps the design, the runner approves the plan and
  it runs in the same run. Otherwise (a risky revision, one that adds, removes or reorders tasks,
  no key, Jev down) the plan stays held, with Jev's flag shown when it held it. Compare the original
  and the proposed revision on Approvals; accept the revision, keep the original, or abandon. If you
  resolve it first, the runner leaves it alone.
- **Blocked at the smoke test**: the finished feature failed in the running environment and the
  fix rounds ran out, or the environment did not start. Read the report; **Try again** runs one more
  smoke test with fresh rounds, **Accept as is** closes the plan, **Abandon** drops it.
- **A wave does not move**: a feature waits for the plans it depends on. Find the first one not
  done on the wave's page.

## Review when you like

**To review when you like** never blocks anything:

- **Review after merge**: merged work of the features you review forward. "Reviewed" or "Needs a fix…"
  (your reason becomes a fix task in the maintenance plan).
- **Loose ends** (shown as review after merge): any merged task the reviewer passed with medium findings, or
  whose handoff reports a plan item partial or skipped ("not verified"). They used to vanish; now
  they wait here, and the retro turns the ones still open at the end of a wave into a debt plan.
- **Conventions** proposed by plans or by the retro.
- **Deviations** (decisions that departed from a plan or a convention) are here while the
  triage is off, and when it escalates one (below). With it off: accept (it becomes a rule later plans respect) or reject (a task to undo it is
  queued). With it on, the classifier judges them and they leave this list: read the verdicts on the
  **Triage** page (who judged, the answers, what happened next). One whose rounds ran out stays
  proposed, off the queue, and shows in insights: align through a convention, a plan or a gate.
  A deviation that followed one rule against another (the task asked for one thing, a convention or
  a gate for another) is **escalated**: it stays proposed and stays in this list, labelled *rule
  conflict*, without blocking. No undo task is queued; fix the rule, then accept or reject it. A
  deviation that also weakens a test is rejected instead: coverage is never traded for a rule conflict.

Fix and undo tasks land in the **maintenance plan**, which you approve once; after that new ones
run without asking.

## The run stops by itself

| `run` says | What happened | Do |
|---|---|---|
| `context API unreachable … waiting` | the context is restarting | nothing: it waits (`AUTONOME_CONTEXT_WAIT_S`, 5 min) and resumes |
| `context API 500 on …; stopping the runner` | the context failed for real | check `docker compose logs context`; the task went back to pending; run again |
| `usage limit: … run again after the reset` | the account's usage limit or monthly spend limit | run again after the reset (or raise the limit), or use `--watch`, which waits for it; the task went back to pending |
| `#N failed unexpectedly: …` | the runner itself threw (a bug, not the task) | nothing: the task went back to pending with an `error` run; the third error in a row blocks it as `runner error (3 in a row)` |
| `… is merged into main; it will be recorded as done on the next run` | main moved but the context could not hear it | run `run` again: it records the merge first |
| `autonome doctor found problems; not starting` | see "Before a run" | |
| `nothing to run` | no approved task is ready | approve a plan, or check Approvals for what blocks |

## Cleaning up

Blocked and abandoned tasks leave their branch, and an interrupted run can leave a worktree:

```bash
git worktree list                        # worktrees live in ../.autonome-worktrees/
git worktree prune                       # forget the ones whose folder is gone
git worktree remove --force <path>       # remove one you no longer need
git branch --list 'autonome/task-*'      # task branches; delete the ones you have read
git branch -D autonome/task-<id>-<suffix>
```

The plugin's own state and logs (handoff writer, gate hook) are in `~/.autonome/`; the
`state/merged-*.json` files are merges waiting to be recorded, so leave them.

## After updating autonome

A release that renames things (statuses, gate config keys, endpoints) is a single cut, with no
aliases or data migration. The memory of a test setup is reset, then the context and the plugin are
rebuilt:

```bash
docker compose down -v                          # drops the Postgres volume: back up first if it matters
docker compose up -d --build context            # rebuild the context; migrations run on a clean database
claude plugin update autonome@autonome          # then start a new Claude session
```

## When the memory needs care

- Back up before anything destructive: see [configuration](configuration.md#5-keeping-the-memory-safe).
- A version saved while embeddings failed is stored without chunks:
  `cd context && uv run autonome-context reindex` indexes what is missing.
- `docker compose logs context` shows migrations applied at start and any failed request.
- Saves get slow (seconds to a minute for a large plan): the Ollama container is embedding on the
  CPU. Use [embeddings on the host](configuration.md#embeddings-on-the-host).

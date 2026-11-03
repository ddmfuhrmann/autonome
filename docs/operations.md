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
| `✘ gates no gates configured` | run `/autonome:init` and save its proposal in Settings, or approve a plan that brings gates (see [quick start](quickstart.md) and [configuration](configuration.md)) |
| `✘ gates not found in …: <cmd>` | install the tool, or fix the gate's `run` |
| `✔ triage warning: no Jev key …` | the triage is on with `jev-latest` but the runner has no key; the fallback judges. Put `TYPESAFE_API_KEY` in `~/.autonome/secrets.env` (`chmod 600`) or the runner's environment |
| `✘ main uncommitted changes` | commit or stash on the main checkout: the runner never merges over local work |

`run` runs these checks itself and refuses to start while one fails.

After the checks, once per run, the runner tells the context whether it found the Jev key (in its
environment or in `~/.autonome/secrets.env`). Only that yes/no is sent, never the key. The triage
section of Settings shows it as found, missing, or unknown since when the runner last reported. If the
report fails (context down, an error answer), the runner logs a `warning:` line and the run goes on.

### Turning on auto permissions

`permissions: auto` in the gate config lets the implementer's sessions run scripts, judged by the
Agent SDK's classifier (see [configuration](configuration.md#permissions)). Before turning it on in
a project:

```bash
plugin/bin/autonome doctor --project <slug> --repo <path>   # the implementer's model must support auto
plugin/bin/autonome bench implementer/permissions            # costs tokens
```

The bench tries known cases (a scripted rename, a denied git write, a read of `~/.autonome`, an
exfiltration attempt). Turn the key on only if it holds. Git history, `~/.autonome` and paths outside
the worktree stay denied in both modes. To go back, set `permissions: allowlist`.

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

### What happens before a task blocks: the staff

When a task is about to block, the runner first calls the **staff**, a senior agent (Opus, high effort
by default, `models.staff`). It runs **once per attempt**: **Try again** opens a new attempt, so it
can look again. It starts a new session in the task's own worktree, with the reason, the gate output,
the open findings, the recent history of `main` and what the implementer last said.

It does not enter for the usage limit, a `runner error`, `conflicts with main` or a closed pull
request; those keep the handling above. It also does not start on a paused queue.

It answers with one of four actions:

| Action | What the runner does |
|---|---|
| `fixed` | the staff changed the worktree; the gates and the reviewer run again, and only a pass goes on to merge |
| `already satisfied` | the task asks for what `main` already has. The staff names the commit (`satisfied_by`); the runner checks it is in `main` and the worktree has no changes, Jev judges it, and the task is `done` **without a merge**, with the commit as proof |
| `revise` | the task's spec cannot be met as written. The staff proposes a new title, spec and acceptance; Jev judges it, the proposal is saved on the task, and the implementer runs the revised task, then the gates and the reviewer |
| `give up` | nothing above applies; the task blocks with the staff's diagnosis |

Jev judges what changes scope. For an already satisfied task he asks whether the commit delivers what
the task asked. For a revision he asks whether it drops an acceptance, adds something out of scope or
changes the design. If any answer reaches `reject_at`, the verdict is hold: the task blocks with the
staff's diagnosis and the flag, and the proposal is on the task's page; read it, then **Try again** or
edit the task.

Without the Jev key (or with `triage` off), or when his call fails, the staff can still fix. Closing
or revising becomes a diagnosis only: the task blocks as `staff: <diagnosis>`, and a failed call never
counts as accepted. A staff session that fails or answers badly also blocks the task, with the
original reason and the error.

Before the runner removes the worktree of a blocked task (or one that stopped on the usage limit), it
commits the changes as `wip(task-N): <reason>` on the task's branch and records its sha in the run
(`wip_sha`); the WIP never reaches `main`. To see it: `git log main..<branch>` (the branch is on the
task's page) and `git show <wip_sha>`.
**Try again** starts a new session in a fresh worktree from `main` with that work applied, and the
prompt says why the last attempt stopped. It starts clean, with no WIP, when the work no longer
applies on `main` (the session is told which branch holds it), when the run has no `wip_sha` (nothing
changed, or the WIP commit failed, in which case the worktree is kept) and after a pause (a paused
task resumes its own session and worktree).

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
  smoke test with fresh rounds, **Accept as is** closes the plan, **Abandon** drops it. Criteria the
  tester could not exercise show as `untested` with the reason; they never block the plan, but read
  them: if the tester was denied what it needed, adjust `permissions` or the environment.
- **A wave does not move**: a feature waits for the plans it depends on. Find the first one not
  done on the wave's page.

## Review when you like

**To review when you like** never blocks anything:

- **Review after merge**: merged work of the features you review forward. "Looks right" or "Needs a fix…"
  (your reason becomes a fix task in the maintenance plan).
- **Loose ends** (shown as review after merge): any merged task the reviewer passed with medium findings, or
  whose handoff reports a plan item partial or skipped ("not verified"). They used to vanish; now
  they wait here, and the retro turns the ones still open at the end of a wave into a debt plan.
  Low findings that merged do not wait here (too many to read one by one): the retro reads them as
  `open_debt` and folds the real ones into the same debt plan, grouped by file or feature.
- **Conventions** proposed by plans or by the retro.
- **Deviations** (decisions that departed from a plan or a convention) are here while the
  triage is off, and when it escalates one (below). With it off: accept (it becomes a rule later plans respect) or reject (a task to undo it is
  queued). With it on, the classifier judges them and they leave this list: read the verdicts on the
  **Triage** page (who judged, the answers, what happened next). One whose rounds ran out stays
  proposed, off the queue, and shows in insights: align through a convention, a plan or a gate.
  One that only repeats a choice you already made on the same feature (`repeats_human_choice`) is
  accepted as your choice, with no undo task; the Triage page shows it as accepted as the human's choice.
  A choice you make in the session itself (you answer an options question, approve a proposal) is
  marked `human_choice` by the handoff writer, with your words as the quote, only when the transcript
  shows it. That decision enters accepted and the triage never screens, judges or undoes it. To undo
  it, reject it yourself in the UI; that queues the undo task as for any decision.
  A deviation that followed one rule against another (the task asked for one thing, a convention or
  a gate for another) is **escalated**: it stays proposed and stays in this list, labelled *rule
  conflict*, without blocking. No undo task is queued; fix the rule, then accept or reject it. A
  deviation that also weakens a test is rejected instead: coverage is never traded for a rule conflict.

Fix and undo tasks land in the **maintenance plan**, which you approve once; after that new ones
run without asking.

Schedules of `kind: simplify` add to it too: a read-only agent looks at the code that changed since
the last round and queues up to `max_tasks` simplification tasks, each with the evidence. It never
edits code; the task does, through the runner. If it queues nothing, check the runner's log for
`simplify failed`.

## The run stops by itself

| `run` says | What happened | Do |
|---|---|---|
| `context API unreachable … waiting` | the context is restarting | nothing: it waits (`AUTONOME_CONTEXT_WAIT_S`, 5 min) and resumes |
| `context API 500 on …; stopping the runner` | the context failed for real | check `docker compose logs context`; the task went back to pending; run again |
| `usage limit: … run again after the reset` | the account's usage limit or monthly spend limit | run again after the reset (or raise the limit), or use `--watch`, which waits for it; the task went back to pending |
| `queue paused by <by>: <reason>` | the project's queue is paused; the runner takes no task (with `--watch` it waits) | a human resumes the queue. A session running when the pause came is interrupted and the task is `paused`, with its worktree and branch kept; after the resume it goes on in the same session (a new session on the same branch if the worktree is gone) |
| `#N failed unexpectedly: …` | the runner itself threw (a bug, not the task) | nothing: the task went back to pending with an `error` run; the third error in a row blocks it as `runner error (3 in a row)` |
| `… is merged into main; it will be recorded as done on the next run` | main moved but the context could not hear it | run `run` again: it records the merge first |
| `SIGINT: stopping the runner and its sessions; …` | you pressed Ctrl-C (or the runner got a SIGTERM) | nothing: the sessions and the docs agent are killed with it (a second Ctrl-C exits at once). The next `run` puts the task back to pending with its work as WIP on its branch (`#N was left running by a runner that stopped`), and the task starts from it |
| `another runner is already running for <project> (pid N); stop it first` | one runner per project on a machine (`~/.autonome/state/run-<project>.lock`) | stop the other one, or let it work; a lock left by a runner that died is taken over |
| `autonome doctor found problems; not starting` | see "Before a run" | |
| `nothing to run` | no approved task is ready | approve a plan, or check Approvals for what blocks |

## How a task ends

When the implementer finishes and the work is committed, the runner goes through these steps in order:

1. **Commit** the task's work on its branch. The subject is the `Commit: <subject>` line the implementer
   ends its session with (type from its prefix, scope always the task's feature, up to 72 characters); without
   a usable one, the task's title.
2. **Rebase** onto the current `main`.
3. **Gates and handoff together**: all the gates run on top of `main` while the handoff writer reads the
   task's diff and writes the handoff. Nothing changes in the worktree meanwhile, so the handoff sees
   the task's own diff. The two run side by side, so the task waits for the slower one, not both.
4. **Triage** screens the decisions the handoff brought (when it is on). It needs the handoff, so it
   comes after step 3 and before the merge.
5. **Merge**: fast-forward of `main` (or the pull request, in PR mode). A pause is checked before
   this step, as before each of the others.
6. **Living doc**: the merge only marks the feature. Its doc is rewritten once the queue runs dry
   (the run ends, or `--watch` has nothing to play), once per feature however many of its tasks merged,
   and the system overview after it. A plan of ten tasks costs one doc, not ten.

A task that fails or blocks does not take this path: it stays sequential. When the queue runs dry, the
runner waits for the docs, up to 5 minutes, so no process is left behind. A doc that fails or is cut off
does not undo the merge; the `doc-drift` schedule catches it later.

## What the runner limits in a session

A command stuck in the implementer's session must not just be waited on:

- The Bash tool is cut at 5 minutes (`BASH_MAX_TIMEOUT_MS`, set in the session's environment), not the
  default 10.
- Running a command in the background and the `Monitor` tool are denied, in both permission modes. The
  session cannot park a command and wait for it.
- The prompt tells the implementer to investigate a command that passes 2 minutes (what it is waiting
  on, a stack trace) instead of waiting.
- A build or check that really takes longer than 5 minutes should be a gate: gates have their own
  `timeout_s` and are not cut by the session's Bash limit.
- In autonome's own tests, pytest prints the traceback of a test stuck for more than 60 s
  (`faulthandler_timeout`). It only prints; the test is not failed.

## What the session is told about its machine

Commands that fail only because of where they run cost turns in every task. So:

- The implementer's and the staff's prompts say the session starts at the worktree root, and paths
  are given from there instead of `cd`. On macOS they also say the tools are BSD (`sed -i ''`). With
  zsh as the shell they say to quote globs. The runner reads these from the machine it runs on.
- The implementer's prompt says to copy an Edit's `old_string` from a Read of those lines.
- The session start hook checks the files the briefing names in code spans against the worktree. A
  file the briefing names from another directory (`telas/painel.html` for
  `docs/prototipo-ui/telas/painel.html`) gets its real path. A file the repo does not have is listed
  as not there: the implementer creates it only when the task asks for it, and otherwise does not
  look for it.

## Pausing the queue

Pause when something wrong is about to merge or you need the runner to stop touching the project
(a bad plan, a task heading the wrong way). Anyone can pause: the UI's Pause button, or an agent
with `queue_pause`. Only a human resumes.

- **Nothing new starts.** The runner takes no task while the queue is paused (`--watch` waits).
- **The task in progress is interrupted**, not killed: its status becomes `paused`, and its
  worktree and branch are kept, along with the session. A pause that comes before the merge stops
  it from merging.
- **Resume** in the UI (Resume button) or with `queue_resume`, which asks you in a dialog the agent
  cannot answer. Paused tasks go back to `pending` and continue in the same session.
- **Or abandon** the paused task in the UI, if it should not continue.
- **The resume only works on the same machine**: the session and the worktree live in the runner's
  machine. Elsewhere (or if the worktree is gone) the task starts a new session on its branch.

A paused task shows in Approvals as "waiting on you" (`paused`), with Resume and Abandon.

## Pull request mode

With `merge: {mode: pr, pr: manual|auto}` in the gate config (default `ff`: fast-forward main), the
runner does not move main. After the gates pass on top of main, and if the queue is not paused, it
runs `git push -u origin <branch>` and `gh pr create --base main --head <branch>`, then the task
becomes `in_pr` with the PR link (queue and Approvals). The worktree is removed; the branch stays.

- **`manual`**: you merge the PR on GitHub; the task shows as "waiting on you".
- **`auto`**: the runner runs `gh pr merge --auto --rebase`, so GitHub merges when the checks pass.
  If GitHub refuses auto-merge, the PR stays open for you and the log says so.
- **Following**: on each turn of the loop (and at the start of a run) the runner reads each `in_pr`
  task's PR with `gh pr view`. Merged: main is fetched and fast-forwarded when it is clean, and the
  task is `done` with the merge commit. Closed without merge: the task is `blocked`. Open: nothing.
- **`gh` missing, not logged in or offline** is infrastructure: a failed push or `pr create` sends the
  task back to `pending` (work kept on its branch), a failed `gh pr view` warns and is tried again on the next turn; the third failure in a row blocks the
  task (only you move `in_pr` back to `pending`). `doctor` checks
  `gh auth status` when the mode is `pr`. Only the runner calls `gh`; sessions never get it.
- Only GitHub (`gh`) is supported; the runner does not read or answer PR comments.

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

The runner is built by `plugin/bin/autonome` on first use and again whenever a file in the runner's
`src`, its `package.json` or its `pnpm-lock.yaml` is newer than the build. The build clears `dist`
first, so tests left from a renamed or removed source do not run.

## When the memory needs care

- Back up before anything destructive: see [configuration](configuration.md#5-keeping-the-memory-safe).
- A version saved while embeddings failed is stored without chunks:
  `cd context && uv run autonome-context reindex` indexes what is missing.
- `docker compose logs context` shows migrations applied at start and any failed request.
- Saves get slow (seconds to a minute for a large plan): the Ollama container is embedding on the
  CPU. Use [embeddings on the host](configuration.md#embeddings-on-the-host).

# Quick start

From zero to a first run. Everything starts with `/autonome:init`.

Prerequisites: Docker, Claude Code, Node 20+ with pnpm (for the runner).

## 1. Start the context

```bash
git clone https://github.com/ddmfuhrmann/autonome.git && cd autonome
docker compose up -d --wait     # postgres, ollama and the context (UI at http://127.0.0.1:8765/ui)
```

## 2. Install the plugin

```bash
claude plugin marketplace add ddmfuhrmann/autonome
claude plugin install autonome@autonome
```

## 3. Open Claude Code in your project

`cd` into the repo of the project you want to build with autonome and start `claude`.

## 4. Run `/autonome:init`

The skill reads your repo (stack, scripts, CI, `compose.yml`, folders), checks the machine, and asks
only what the repo does not answer. It builds the gates from commands your repo already runs, never
invented ones. At the end it proposes:

- the **settings** (gate config);
- the **feature catalog**;
- the first **conventions**.

It never writes to your repo and never installs anything.

### What it checks on the machine

It runs `autonome doctor` and tells you what is missing. You fix it; the init does not.

| Missing | What to do |
|---|---|
| Docker or the context does not answer | start Docker, then `docker compose up -d --wait` in the autonome repo |
| Jev key (`TYPESAFE_API_KEY`) | optional, for the triage. Get a key at console.typesafe.ai, then `echo 'TYPESAFE_API_KEY=...' >> ~/.autonome/secrets.env && chmod 600 ~/.autonome/secrets.env`. Without it, the Haiku fallback judges |
| `gh` | only needed for `merge.mode: pr`. Install the GitHub CLI and run `gh auth login`, or keep merging directly |

More on each doctor message: [operations](operations.md#before-a-run).

## 5. Review and save the settings

The init gives you a link to the Settings. Read the proposal and save it there. Only you save it;
the agent cannot.

## 6. Accept the conventions

Open the proposed conventions in the UI and accept the ones you agree with. Details:
[conventions](conventions.md).

## 7. Plan

```
/autonome:plan
```

Talk through the first feature with the agent. It ends in a draft you approve.

## 8. Run

```
/autonome:run
```

The runner runs every approved task, one session and worktree each, and merges when the gates pass.

## 9. Watch

```
/autonome:watch
```

A line per change, a notification for what needs you, and each waiting item discussed in the chat.

## Next

- [Configuration](configuration.md): every key of the gate config.
- [Operations](operations.md): what to do when something stops.
- [plugin/README.md](../plugin/README.md): the whole cycle.

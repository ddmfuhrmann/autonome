# autonome plugin

The recommended process for [autonome-context](../context), as a Claude Code plugin. You take part
in one step, the plan; everything after approval runs without you.

## The cycle

| Step | How | Who |
|---|---|---|
| **compose** | `/autonome:compose` | You and the composer write the plan, grounded in the project's memory, or a **wave**: one plan per feature, grouped in play order. It reads what went off plan before on the same subject (`history_get`) and turns each lesson into acceptance, out of scope, a convention or a question; it walks each task's edge cases; it proposes the conventions and gates the project lacks. Where a PRD is required (a backlog item flagged `prd`, or what the project's CLAUDE.md says) and missing, it writes the PRD with you first, in the language the project uses for product docs. It is saved as a draft. |
| **approve** | in the session, or the context UI | You, a plan or a whole wave at once (with any gates it adds). In the session, the context asks you directly in a dialog (MCP elicitation) that the agent cannot answer; agents have no way to approve. |
| **conduct** | `/autonome:conduct` or `autonome conduct --project <slug> [--watch]` | The maestro plays each approved task in its own session and worktree, and merges to `main` only when the gates pass. Before a wave feature that waited for others, it **rehearses** the plan against `main`: notes for the implementers, or a revision held for you. `--watch` keeps it waiting for new approved work. |
| **review** | automatic | Before each merge, an agent that did not write the code checks the diff against the plan, its edges and the conventions. Blocking findings, and anything an active convention requires, send the implementer back; medium findings go back once (fix, or explain in the handoff), later rounds check the earlier findings and raise only new high ones, and what is still open at merge waits for you as a loose end. |
| **soundcheck** | automatic, if configured | When a plan's last task merges, the feature is tried in a running environment: scripted checks, then a listener agent exercising the acceptance criteria. Failures become fix tasks; repeated failures block the plan for you. |
| **playback** | Approvals → "To review when you like" | Review forward, for the features you list: they merge as usual and wait for you in the background. "Sounds right", or "Needs a fix…" with a reason, which becomes a fix task. Never blocks. |
| **fermata** | the context UI | The opposite choice, for projects that want it: a critical task stops before merge until you release it. |
| **tune** | automatic after each wave, or `/autonome:tune` | A retrospective on the wave: reviewer findings, what merges left open, encores, cost, gate failures, friction and the briefing's weight. It proposes, for you to approve: conventions and gates; a debt plan for open loose ends; process config changes (allowed commands, soundcheck, models, budget) as YAML in its report on the wave page; and notes on autonome itself. |
| **watch** | `/autonome:watch` or `autonome watch --project <slug> [--once]` | Follows a run from a Claude session: a line per change, no model in the loop, a push notification for what needs you. Each waiting item comes into the chat with what stopped and a recommendation; you discuss it and confirm the decision in a dialog. `conduct` closes the same way when it stops. |
| **cues** | `autonome cues --project <slug>` | Scheduled checks that enqueue maintenance tasks. Run it from cron. |

Every session, interactive or not, ends in a **handoff** written by a separate agent: plan vs.
implementation vs. gates, deviations, decisions and friction. It goes to the context, where it is
redacted, chunked and indexed.

## Where it acts

Only in projects set up for it: the project has a gate config saved in the context (UI → Config),
or `AUTONOME_PROJECT` is set. Everywhere else the hooks stay silent and cost nothing: no briefing at
the start of a session, no handoff at the end.

## What's in here

| Path | What |
|---|---|
| `skills/` | `compose`, `conduct`, `watch`, `tune`, and `handoff` (the scribe) |
| `hooks/` | `SessionStart` injects the task and conventions; `Stop` is the metronome; `SessionEnd` writes the handoff |
| `lib/autonome/` | Shared hook code. Python standard library only, so there is nothing to install. |
| `runner/` | The maestro (TypeScript, Claude Agent SDK). `bin/autonome` builds it on first use. |
| `evals/` | Behaviour evals for the skills (`claude plugin eval`) |

## The metronome

The `Stop` hook runs the project's gates, cheapest first, and sends failures back to the agent as an
*encore*. If the same failure repeats, or the budget runs out, the task is blocked instead of looping.

- It always runs in conduct sessions and **fails closed**: if the context is down, nothing merges.
- It runs in interactive sessions only when the project's gate config sets `interactive: true`.
- The gate rules (linters, tests, Semgrep, ArchUnit…) live in your repo. The context only stores
  which ones run, in what order and how strictly, and you edit that in the UI.

## Configuration

| Variable | Default | What |
|---|---|---|
| `AUTONOME_CONTEXT_URL` | `http://127.0.0.1:8765` | Where the context runs |
| `AUTONOME_PROJECT` | git repo folder name | The project's slug in the context |
| `AUTONOME_HANDOFF_MODEL` | `sonnet` | Model the scribe uses |
| `AUTONOME_MIN_USER_TURNS` | `3` | Shorter sessions get no handoff |

Hooks never break your session: when the context is unreachable they step aside, except the
metronome during conduct.

## The soundcheck

When every task of a plan has merged, the maestro can try the feature in a running environment:
`up` starts it and waits until it answers, the `checks` run against `AUTONOME_APP_URL`, the listener
agent exercises the plan's acceptance criteria with curl, and `down` always runs. Use fakes and
synthetic data, never real external services. It is configured in the gate config (UI → Config).

A FastAPI service, with the API of shortlink (a test bed project) as the example:
a throwaway SQLite file, a port of its own, and one scripted check per behaviour that matters.

```yaml
soundcheck:
  # A throwaway database and a port of its own, so it never touches your dev data or server.
  up: |
    export SHORTLINK_DB="${TMPDIR:-/tmp}/shortlink-soundcheck.db"; rm -f "$SHORTLINK_DB"
    uv run uvicorn shortlink.app:app --port 18431 < /dev/null > "${TMPDIR:-/tmp}/shortlink-soundcheck.log" 2>&1 &
    echo $! > "${TMPDIR:-/tmp}/shortlink-soundcheck.pid"
    for i in $(seq 30); do curl -sf http://127.0.0.1:18431/health > /dev/null && exit 0; sleep 1; done
    cat "${TMPDIR:-/tmp}/shortlink-soundcheck.log"; exit 1
  down: |
    kill "$(cat "${TMPDIR:-/tmp}/shortlink-soundcheck.pid" 2>/dev/null)" 2>/dev/null; pkill -f "uvicorn shortlink.app:app --port 18431"
    rm -f "${TMPDIR:-/tmp}/shortlink-soundcheck.db" "${TMPDIR:-/tmp}/shortlink-soundcheck.pid"; true
  url: http://127.0.0.1:18431
  checks:
    - name: api
      run: |
        set -eu
        u="$AUTONOME_APP_URL"
        fail() { echo "FAIL: $*"; exit 1; }
        body=$(curl -s -w '\n%{http_code}' -X POST "$u/links" -H 'content-type: application/json' -d '{"url": "https://example.com/soundcheck"}')
        [ "$(echo "$body" | tail -1)" = 201 ] || fail "POST /links: expected 201, got $body"
        code=$(echo "$body" | head -1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')
        r=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$u/$code")
        [ "$r" = "302 https://example.com/soundcheck" ] || fail "GET /$code: expected 302 to the url, got $r"
        clicks=$(curl -s "$u/links/$code/stats" | python3 -c 'import json,sys; print(json.load(sys.stdin)["clicks"])')
        [ "$clicks" = 1 ] || fail "GET /links/$code/stats: expected clicks 1, got $clicks"
        s=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$u/links" -H 'content-type: application/json' -d '{"url": "ftp://example.com/x"}')
        [ "$s" = 422 ] || fail "POST /links with ftp://: expected 422, got $s"
        s=$(curl -s -o /dev/null -w '%{http_code}' "$u/doesnotexist")
        [ "$s" = 404 ] || fail "GET of an unknown code: expected 404, got $s"
        echo "api: 5 checks passed"
  listener: true
  max_fixes: 2
```

Keep `up` idempotent (it removes the old database) and `down` forgiving (it ends in `true`): a
soundcheck that cannot start blocks the plan with nothing to fix.

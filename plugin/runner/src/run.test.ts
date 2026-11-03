import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { consult } from "./agents.js";
import { git } from "./git.js";
import { UsageLimitError } from "./limits.js";
import { implement, implementerPermissions, implementerTools, judgeTask,preflightPlan, recordRunnerError, runTask, verdictLine, type JudgeSeams, type RunOptions } from "./run.js";
import type { Finding, GateConfig, Plan, ReviewResult, Run, Task } from "./context.js";
import { earlierFindingsSection, gatesSection, recordRound, retryMessage, settleOutcomes, settleRun } from "./review.js";

const realFetch = globalThis.fetch;
const realKey = process.env.TYPESAFE_API_KEY;
const realHome = process.env.AUTONOME_HOME;
afterEach(() => {
  globalThis.fetch = realFetch;
  if (realKey === undefined) delete process.env.TYPESAFE_API_KEY; else process.env.TYPESAFE_API_KEY = realKey;
  if (realHome === undefined) delete process.env.AUTONOME_HOME; else process.env.AUTONOME_HOME = realHome;
});

/** A fake context for the preflight path: the preflight holds the plan, one revision waits for the triage. */
function preflightContext(shapeChanged = false) {
  const calls: { method: string; url: string; body?: any }[] = [];
  const json = (b: unknown) => new Response(JSON.stringify(b), { headers: { "content-type": "application/json" } });
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.endsWith("/preflight")) return json({ plan_status: "held" });
    if (url.endsWith("/gate-config")) {
      return json({ config: { triage: { model: "jev-latest", fallback: "haiku", reject_at: 0.7, max_rounds: 2 } } });
    }
    if (url.endsWith("/triage")) {
      return json({ deviations: [], screen: [], revisions: [{ plan_id: 677, state: { original: [], revised: [] }, shape_changed: shapeChanged }] });
    }
    return json({ status: "ok" });
  };
  return calls;
}

const plan = { id: 677, body: { title: "shortlink-v6" } } as unknown as Plan;
const task = { id: 4, body: { title: "t" } } as unknown as Task;
const preflightOpts = (log: string[]) => ({ project: "p", repo: ".", maxTasks: 1, push: false, log: (l: string) => log.push(l) }) as RunOptions;
const fakePreflight = async () => ({ verdict: "revise" }) as never;
const noScores = { drops_acceptance: 0.01, adds_out_of_scope: 0.02, changes_design: 0.13 };

test("a preflight that holds the plan is judged by Jev; an accept starts the plan in the same run", async () => {
  process.env.TYPESAFE_API_KEY = "k";
  const calls = preflightContext();
  const log: string[] = [];
  const ready = await preflightPlan(plan, task, preflightOpts(log), { preflight: fakePreflight, ask: async () => noScores });
  assert.equal(ready, false); // the claim loop goes round again, and the approved plan is claimed
  const post = calls.find((c) => c.url.endsWith("/api/plans/677/preflight-triage"))!;
  assert.equal(post.method, "POST");
  assert.equal(post.body.verdict, "accept");
  assert.deepEqual(post.body.answers, { drops_acceptance: "0.01", adds_out_of_scope: "0.02", changes_design: "0.13" });
  assert.ok(calls.findIndex((c) => c.url.endsWith("/tasks/4/transition")) < calls.indexOf(post)); // handed back first
  assert.ok(log.includes("triage P677: accept (jev, changes_design 0.13)"));
});

test("a revision that changed shape does not call Jev and stays held", async () => {
  process.env.TYPESAFE_API_KEY = "k";
  const calls = preflightContext(true);
  const log: string[] = [];
  let asked = 0;
  await preflightPlan(plan, task, preflightOpts(log), { preflight: fakePreflight, ask: async () => { asked++; return noScores; } });
  assert.equal(asked, 0);
  assert.ok(!calls.some((c) => c.url.endsWith("/preflight-triage")));
  assert.ok(log.includes("triage P677: shape changed, left for you"));
});

test("without a Jev key the plan stays held and the run goes on", async () => {
  delete process.env.TYPESAFE_API_KEY;
  process.env.AUTONOME_HOME = mkdtempSync(join(tmpdir(), "autonome-nokey-"));
  const calls = preflightContext();
  const log: string[] = [];
  const ready = await preflightPlan(plan, task, preflightOpts(log), { preflight: fakePreflight });
  assert.equal(ready, false);
  assert.ok(!calls.some((c) => c.url.endsWith("/preflight-triage")));
  assert.ok(log.some((l) => l.startsWith("triage P677: left for you: ")));
  assert.ok(log.includes("  the plan needs revising; held for your approval"));
});

test("every round keeps its findings, each marked with what became of it", () => {
  const f = (severity: "high" | "medium", issue: string): Finding => ({ kind: "missing", severity, issue });
  const rounds: ReviewResult[] = [
    { verdict: "fail", findings: [f("high", "no test"), f("high", "raw 500")] },
    { verdict: "pass", findings: [f("high", "Raw 500 ")] },
  ];
  settleOutcomes(rounds, "merged");
  assert.deepEqual(rounds[0].findings.map((x) => x.outcome), ["resolved", "repeated"]);
  assert.deepEqual(rounds[1].findings.map((x) => x.outcome), ["merged"]);
  settleOutcomes(rounds, "blocked");
  assert.equal(rounds[1].findings[0].outcome, "blocked");
});

test("the run keeps each round as recorded and review stays the last; outcomes settle on the run", () => {
  const f = (issue: string): Finding => ({ kind: "missing", severity: "high", issue });
  const r1: ReviewResult = { verdict: "fail", findings: [f("no test")] };
  const r2: ReviewResult = { verdict: "pass", findings: [] };
  const run: Run = { at: "", branch: "", worktree: "", base: "", outcome: "" };
  recordRound(run, r1, 1);
  recordRound(run, r2, 2);
  assert.deepEqual(run.reviews, [r1, r2]);
  assert.equal(run.review, r2);
  assert.equal(run.review_rounds, 2);
  settleRun(run, "merged");
  assert.equal(r1.findings[0].outcome, "resolved"); // fixed in round 2: not reported again
  settleRun({}, "merged"); // review off: nothing to settle
});

test("a block that is not the reviewer's leaves the last round's findings unmarked", () => {
  const medium: Finding = { kind: "missing", severity: "medium", issue: "weak test" };
  const run: Run = { at: "", branch: "", worktree: "", base: "", outcome: "" };
  recordRound(run, { verdict: "pass", findings: [medium] }, 1);
  settleRun(run, undefined); // e.g. the merge failed
  assert.equal(medium.outcome, undefined);
});

/** Runs the real review loop of the run over a temp repo, with a scripted reviewer and fake gates. */
async function runLoop(rounds: ReviewResult[], maxRounds: number, briefings: string[] = []) {
  const dir = mkdtempSync(join(tmpdir(), "judge-"));
  const sh = (...a: string[]) => execFileSync("git", a, { cwd: dir, encoding: "utf8" }).trim();
  sh("init", "-q", "-b", "main");
  sh("config", "user.email", "t@t");
  sh("config", "user.name", "t");
  writeFileSync(join(dir, "a.txt"), "one\n");
  sh("add", "-A");
  sh("commit", "-q", "-m", "base");
  const base = sh("rev-parse", "HEAD");
  writeFileSync(join(dir, "a.txt"), "two\n");

  const run: Run = { at: "", branch: "", worktree: dir, base, outcome: "" };
  const task = { id: 1, project: "p", body: { title: "t", spec: "s" } } as unknown as Task;
  const cfg = { review: { enabled: true, max_rounds: maxRounds }, retries: { max: 1 }, models: {} } as unknown as GateConfig["config"];
  const opts = { project: "p", repo: dir, maxTasks: 1, push: false, log: () => {} } as RunOptions;
  let call = 0;
  let retries = 0;
  const seams: JudgeSeams = {
    gates: () => ({ passed: true, summary: "all gates passed", outcomes: [
      { gate: "lint", passed: true, output: "" },
      { gate: "test", passed: true, output: "12 passed\nTOTAL    210    9   96%\n" },
    ] }),
    briefing: async () => "briefing",
    // A fresh copy per call, as a real reviewer returns: rounds never share an object.
    review: (async (_task: Task, briefing: string) => {
      briefings.push(briefing);
      return { result: structuredClone(rounds[Math.min(call++, rounds.length - 1)]), cost: 0 };
    }) as JudgeSeams["review"],
  };
  const judged = await judgeTask({ task, cfg, opts, run, worktree: dir, base, current: () => ({ sessionId: "", stop: "" }),
    retry: async () => { retries++; } }, seams);
  return { run, judged, retries: () => retries };
}

test("the review loop keeps every round; fixed findings resolve and the last round's merge", async () => {
  const f = (severity: "high" | "medium" | "low", issue: string): Finding => ({ kind: "missing", severity, issue });
  const r1: ReviewResult = { verdict: "fail", findings: [f("high", "no test")] };
  const r2: ReviewResult = { verdict: "pass", findings: [f("medium", "weak name")] };
  const r3: ReviewResult = { verdict: "pass", findings: [f("low", "nit")] };
  const { run, judged, retries } = await runLoop([r1, r2, r3], 3);
  assert.equal(judged.failure, null);
  assert.equal(judged.blockedOnReview, false);
  assert.equal(retries(), 2); // round 1 high, round 2 medium once; round 3 only has a low one
  const rounds = run.reviews!;
  assert.equal(rounds.length, 3);
  assert.equal(new Set(rounds).size, 3); // each round is its own result
  assert.deepEqual(rounds.map((r) => r.findings[0].issue), ["no test", "weak name", "nit"]);
  assert.equal(run.review, rounds[2]);
  assert.equal(run.review_rounds, 3);
  settleRun(run, "merged");
  assert.deepEqual(rounds.map((r) => r.findings[0].outcome), ["resolved", "resolved", "merged"]);
});

test("the review loop blocks when the reviewer keeps failing until max_rounds", async () => {
  const bad: Finding = { kind: "missing", severity: "high", issue: "raw 500" };
  const fail = (): ReviewResult => ({ verdict: "fail", findings: [{ ...bad }] });
  const { run, judged } = await runLoop([fail(), fail(), fail()], 2);
  assert.match(judged.failure!, /still blocking after 2 retries/);
  assert.equal(judged.blockedOnReview, true);
  assert.equal(run.reviews?.length, 3);
  settleRun(run, judged.blockedOnReview ? "blocked" : undefined);
  assert.deepEqual(run.reviews?.slice(0, 2).map((r) => r.findings[0].outcome), ["repeated", "repeated"]);
  assert.equal(run.review?.findings[0].outcome, "blocked");
});

test("the reviewer gets the gates the runner just ran, so it does not try to run them", async () => {
  const briefings: string[] = [];
  await runLoop([{ verdict: "pass", findings: [] }], 2, briefings);
  assert.equal(briefings.length, 1);
  assert.match(briefings[0], /^briefing\n\n## Gates \(already run on this diff\)\n/);
  assert.match(briefings[0], /- lint: passed\n- test: passed: TOTAL {4}210 {4}9 {3}96%$/);
});

test("the gates section is empty without outcomes and keeps one line per gate", () => {
  assert.equal(gatesSection(undefined), "");
  assert.equal(gatesSection([]), "");
  const long = "x".repeat(500);
  assert.equal(gatesSection([{ gate: "mypy", passed: false, output: `\n${long}\n\n` }]),
    `## Gates (already run on this diff)\n- mypy: failed: ${"x".repeat(200)}`);
});

test("the reviewer's verdict reads as a short list in the live view", () => {
  assert.equal(verdictLine({ verdict: "pass", findings: [] }), "**pass**, no findings");
  assert.equal(verdictLine({ verdict: "pass", findings: [
    { kind: "missing", severity: "low", issue: "No note on\nthe check.", file: "gradle/libs.versions.toml" }] }),
    "**pass**, 1 finding:\n- low missing in `gradle/libs.versions.toml`: No note on the check.");
});

test("medium findings go back while rounds remain; high ones always; low ones never", () => {
  const f = (severity: "high" | "medium" | "low", issue: string) => ({ kind: "missing" as const, severity, issue });
  const mediumOnly = { verdict: "pass" as const, findings: [f("medium", "weak test"), f("low", "nit")] };
  assert.match(retryMessage(mediumOnly, true)!, /passed your diff[\s\S]*- weak test$/);
  assert.ok(!retryMessage(mediumOnly, true)!.includes("nit"));
  assert.equal(retryMessage(mediumOnly, false), null); // rounds spent: it becomes a loose end at merge
  assert.equal(retryMessage({ verdict: "pass", findings: [f("low", "nit")] }, true), null);
  const both = retryMessage({ verdict: "fail", findings: [f("high", "stray file"), f("medium", "weak test")] }, false)!;
  assert.match(both, /blocking issues[\s\S]*stray file[\s\S]*Also worth fixing now[\s\S]*weak test/);
});

function errorsFor(outcomes: string[]) {
  const run = (outcome: string): Run => ({ at: "", branch: "", worktree: "", base: "", outcome });
  const task = { id: 7, body: { title: "t", runs: outcomes.map(run) } } as unknown as Task;
  const calls: { to: string; run?: Run }[] = [];
  const transition = async (_id: number, to: string, run?: Run) => { calls.push({ to, run }); };
  return { task, calls, transition };
}

test("a runner exception sends the task back to pending with the error run recorded", async () => {
  const { task, calls, transition } = errorsFor([]);
  const to = await recordRunnerError(task, new TypeError("x.map is not a function"), transition);
  assert.equal(to, "pending");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].to, "pending");
  assert.equal(calls[0].run?.outcome, "error");
  assert.match(calls[0].run!.reason!, /TypeError: x\.map is not a function/);
});

test("the third runner exception in a row blocks the task", async () => {
  const { task, calls, transition } = errorsFor(["error", "error"]);
  const to = await recordRunnerError(task, new TypeError("boom"), transition);
  assert.equal(to, "blocked");
  assert.equal(calls[0].to, "blocked");
  assert.equal(calls[0].run?.outcome, "error");
  assert.match(calls[0].run!.reason!, /^runner error \(3 in a row\): TypeError: boom/);
});

test("a merged run between two errors resets the count", async () => {
  const { task, calls, transition } = errorsFor(["error", "merged", "error"]);
  assert.equal(await recordRunnerError(task, new Error("boom"), transition), "pending");
  assert.equal(calls[0].to, "pending");
});

const gcWith = (permissions?: string) => ({
  version: 1,
  config: { gates: [{ name: "runner", run: "pnpm test" }], allowed_commands: [], permissions,
    retries: { max: 2, stop_on_repeat: true }, budget: { task_tokens: 1e6, task_minutes: 5, plan_tokens: 1e6 },
    review: { enabled: false, max_rounds: 1 }, smoke_test: null },
}) as unknown as GateConfig;

const GIT_DENIED = ["commit", "push", "reset", "checkout", "switch", "rebase", "merge", "tag", "branch", "stash", "-C"].map((c) => `Bash(git ${c}:*)`);

const SECRET_DENIED = ["Read(~/.autonome/**)", "Edit(~/.autonome/**)", "Bash(cat ~/.autonome:*)"];

test("allowlist, a missing key and an unknown value give today's dontAsk session", () => {
  for (const p of ["allowlist", undefined, "yolo"]) {
    const gc = gcWith(p);
    const perms = implementerPermissions(gc, "/wt");
    assert.equal(perms.permissionMode, "dontAsk");
    assert.equal("permissionPrompts" in perms, false);
    assert.deepEqual(perms.allowedTools, implementerTools(gc));
    assert.equal(perms.hooks.PreToolUse.length, 1);
    for (const d of [...GIT_DENIED, ...SECRET_DENIED]) assert.ok(perms.disallowedTools.includes(d), d);
  }
});

test("auto asks the classifier, never prompts, and keeps the hard limits by rule", () => {
  const gc = gcWith("auto");
  const perms = implementerPermissions(gc, "/wt");
  assert.equal(perms.permissionMode, "auto");
  assert.equal(perms.permissionPrompts, "none");
  assert.deepEqual(perms.allowedTools, implementerTools(gc));
  assert.equal(perms.hooks.PreToolUse.length, 1);
  assert.equal(perms.hooks.PreToolUse[0].matcher, "Bash");
  for (const d of [...GIT_DENIED, ...SECRET_DENIED]) {
    assert.ok(perms.disallowedTools.includes(d), d);
  }
});

test("in every mode the options' hook denies a file command outside the worktree", async () => {
  for (const mode of ["allowlist", "auto"]) {
    const hook = implementerPermissions(gcWith(mode), "/wt").hooks.PreToolUse[0].hooks[0];
    const ask = (command: string) => hook({ tool_input: { command } } as never, undefined, { signal: new AbortController().signal });
    assert.equal(((await ask("rm /etc/x")) as any).hookSpecificOutput.permissionDecision, "deny", mode);
    assert.equal(((await ask("rm -rf build")) as any).hookSpecificOutput.permissionDecision, "allow", mode);
  }
});

test("in every mode the options' hook lets git grep read but never run a command with -O", async () => {
  for (const mode of ["allowlist", "auto"]) {
    const hook = implementerPermissions(gcWith(mode), "/wt").hooks.PreToolUse[0].hooks[0];
    const ask = async (command: string) =>
      ((await hook({ tool_input: { command } } as never, undefined, { signal: new AbortController().signal })) as any).hookSpecificOutput.permissionDecision;
    assert.equal(await ask("git grep -n -iwE 'a|b' -- src"), "allow", mode);
    for (const c of ["git grep -Ovim x", "git grep -nO x", "git grep -iOvim x", "git grep --open-files-in-pager=sh x",
      "git grep --open=sh x", "git grep --op=sh x", "git grep x; rm -rf /", 'git grep x -- "$HOME"']) {
      assert.equal(await ask(c), "deny", `${mode}: ${c}`);
    }
  }
});

test("the implementer's query gets what implementerPermissions returns, and the prompt follows the mode", async () => {
  for (const mode of ["allowlist", "auto"]) {
    const gc = gcWith(mode);
    globalThis.fetch = async () => new Response(JSON.stringify(gc), { headers: { "content-type": "application/json" } });
    let seen: any;
    const fake = (async function* (args: { options: unknown }) { seen = args.options; }) as never;
    const opts = { ...preflightOpts([]), seams: { query: fake } } as RunOptions;
    try {
      await implement(task, "/wt", "main", opts);
    } finally {
      globalThis.fetch = realFetch;
    }
    const expected = implementerPermissions(gc, "/wt");
    for (const k of ["permissionMode", "permissionPrompts", "allowedTools", "disallowedTools"] as const) {
      assert.deepEqual(seen[k], expected[k], `${mode} ${k}`);
    }
    assert.equal(seen.hooks.PreToolUse.length, 1);
    const prompt: string = seen.systemPrompt.append;
    if (mode === "auto") assert.match(prompt, /script or `sed -i`/);
    else assert.match(prompt, /Writing through the shell[\s\S]*is denied/);
    assert.match(prompt, /more than 2 minutes without finishing is investigated, never waited for/);
    assert.equal(seen.env.BASH_MAX_TIMEOUT_MS, "300000", mode);
    assert.equal(seen.env.BASH_DEFAULT_TIMEOUT_MS, "120000", mode);
  }
});

test("in every mode Monitor is off and the hook denies background Bash", async () => {
  for (const mode of ["allowlist", "auto"]) {
    const perms = implementerPermissions(gcWith(mode), "/wt");
    assert.ok(perms.disallowedTools.includes("Monitor"), mode);
    const hook = perms.hooks.PreToolUse[0].hooks[0];
    const ask = (tool_input: object) => hook({ tool_input } as never, undefined, { signal: new AbortController().signal });
    const bg = (await ask({ command: "rm -rf build", run_in_background: true })) as any;
    assert.equal(bg.hookSpecificOutput.permissionDecision, "deny", mode);
    assert.match(bg.hookSpecificOutput.permissionDecisionReason, /background commands are not allowed/);
    assert.equal(((await ask({ command: "rm -rf build" })) as any).hookSpecificOutput.permissionDecision, "allow", mode);
  }
});

test("consult stays read-only dontAsk even when the project turns auto on", async () => {
  let seen: any;
  const fake = (async function* (args: { options: unknown }) {
    seen = args.options;
    yield { type: "result", subtype: "success", total_cost_usd: 0, structured_output: {} };
  }) as never;
  await consult({ cwd: ".", prompt: "p", role: "r", schema: {}, query: fake });
  assert.equal(seen.permissionMode, "dontAsk");
  assert.equal(seen.permissionPrompts, undefined);
  assert.ok(seen.allowedTools.includes("Read"));
  assert.ok(!seen.allowedTools.some((t: string) => t === "Edit" || t === "Write"));
});

test("a later review round gets the earlier findings to check, and no new medium ones", () => {
  assert.equal(earlierFindingsSection(undefined), "");
  const s = earlierFindingsSection([{ kind: "missing", severity: "medium", issue: "weak test", file: "A.java" }]);
  assert.match(s, /^## Earlier findings[\s\S]*- medium missing in A\.java: weak test[\s\S]*only new high findings/);
});

/** A temp repo with a fake context, for runTask over the real git: the sessions and the gates are fakes. */
function wipEnv(commitWip?: RunOptions["seams"] extends infer S ? (S extends { commitWip?: infer C } ? C : never) : never) {
  const parent = mkdtempSync(join(tmpdir(), "wip-"));
  const repo = join(parent, "repo");
  mkdirSync(repo);
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  writeFileSync(join(repo, "a.txt"), "a\n");
  writeFileSync(join(repo, ".gitignore"), "cov.dat\n");
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "init");
  const transitions: { to: string; run?: Run }[] = [];
  const order: string[] = []; // what happened, in order, for the tests of the merge sequence
  let paused: unknown = null;
  let triage: unknown;
  const json = (b: unknown) => new Response(JSON.stringify(b), { headers: { "content-type": "application/json" } });
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/gate-config")) {
      return json({ config: { gates: [], retries: { max: 1 }, review: { enabled: false, max_rounds: 1 }, smoke_test: null, models: {},
        triage, budget: { task_minutes: 5, task_tokens: 1e9, plan_tokens: 1e9 } } });
    }
    if (url.endsWith("/triage")) { order.push("triage"); return json({ deviations: [] }); }
    if (url.endsWith("/state")) return json({ paused });
    if (url.includes("/transition")) transitions.push(JSON.parse(String(init?.body)));
    return json({ status: "ok" });
  };
  const prompts: string[] = [];
  const seen: { cwd: string; files: string[]; status: string }[] = [];
  /** The next session writes `files` in its worktree and then ends with `stop` (a result subtype) or throws `fail`. */
  const session = (files: Record<string, string>, stop?: string, fail?: string) => ((args: any) => {
    prompts.push(args.prompt);
    seen.push({ cwd: args.options.cwd, files: readdirSync(args.options.cwd).filter((f) => f !== ".git").sort(),
      status: git(args.options.cwd, "status", "--porcelain") });
    for (const [f, text] of Object.entries(files)) writeFileSync(join(args.options.cwd, f), text);
    return (async function* () {
      yield { type: "system", subtype: "init", session_id: "s-fake" };
      if (fail) throw new Error(fail);
      if (stop) yield { type: "result", subtype: stop, total_cost_usd: 0 };
    })();
  });
  let query: ReturnType<typeof session> = session({});
  const failGates = () => ({ passed: false, summary: "lint failed: x", outcomes: [] });
  let gatesFn: JudgeSeams["gates"] = failGates;
  const opts = (): RunOptions => ({ project: "p", repo, maxTasks: 1, push: false, log: () => {},
    seams: { query: ((a: unknown) => query(a)) as never, gates: (c, b) => gatesFn(c, b), commitWip } });
  const mk = (runs: Run[] = []) => ({ id: 5, project: "p", status: "pending", feature: null, layers: [],
    body: { title: "t", spec: "s", acceptance: [], plan_id: 1, seq: 1, hold_before_merge: false, runs } }) as unknown as Task;
  const exists = (branch: string) => {
    try { git(repo, "rev-parse", "--verify", `refs/heads/${branch}`); return true; } catch { return false; }
  };
  const mainHasWip = () => git(repo, "log", "main", "--format=%s").split("\n").some((s) => s.startsWith("wip("));
  return { repo, transitions, prompts, seen, mk, exists, mainHasWip, opts, order,
    pause: () => { paused = { by: "test" }; }, withTriage: () => { triage = { model: "haiku", reject_at: 0.7, max_rounds: 1 }; },
    next: (q: ReturnType<typeof session>) => { query = q; },
    gates: (g: JudgeSeams["gates"]) => { gatesFn = g; }, session };
}

const LIMIT_TEXT = "You've hit your session limit · resets 3:40pm";

test("blocking with changes leaves a WIP commit on the branch, without ignored files, and removes the worktree", async () => {
  const env = wipEnv();
  env.next(env.session({ "b.txt": "b\n", "cov.dat": "ignored\n" }));
  const run = (await runTask(env.mk(), env.opts()))!;
  assert.equal(run.outcome, "blocked");
  assert.ok(run.wip_sha);
  assert.deepEqual(env.transitions.map((t) => t.to), ["blocked"]);
  assert.equal(env.transitions[0].run?.wip_sha, run.wip_sha);
  assert.equal(git(env.repo, "rev-parse", run.branch), run.wip_sha);
  assert.equal(git(env.repo, "log", "-1", "--format=%s", run.branch), "wip(task-5): lint failed: x");
  assert.equal(git(env.repo, "show", "--name-only", "--format=", run.branch), "b.txt");
  assert.ok(!existsSync(run.worktree));
  assert.ok(!env.mainHasWip());
});

test("blocking without changes creates no WIP commit", async () => {
  const env = wipEnv();
  const run = (await runTask(env.mk(), env.opts()))!; // the session changes nothing
  assert.equal(run.outcome, "blocked");
  assert.equal(run.wip_sha, undefined);
  assert.equal(git(env.repo, "rev-parse", run.branch), run.base);
  assert.ok(!existsSync(run.worktree));
});

test("a WIP commit that fails keeps the worktree", async () => {
  const env = wipEnv(() => { throw new Error("index.lock exists"); });
  env.next(env.session({ "b.txt": "b\n" }));
  const run = (await runTask(env.mk(), env.opts()))!;
  assert.equal(run.outcome, "blocked");
  assert.equal(run.wip_sha, undefined);
  assert.ok(existsSync(join(run.worktree, "b.txt")));
});

test("the usage limit keeps the work as WIP and does not delete the branch", async () => {
  const env = wipEnv();
  env.next(env.session({ "b.txt": "b\n" }, LIMIT_TEXT));
  const err = await runTask(env.mk(), env.opts()).then(() => null, (e) => e);
  assert.ok(err instanceof UsageLimitError);
  const run = err.run!;
  assert.equal(run.outcome, "usage_limit");
  assert.ok(run.wip_sha);
  assert.equal(git(env.repo, "rev-parse", run.branch), run.wip_sha);
  assert.ok(!existsSync(run.worktree));
  assert.ok(!env.mainHasWip());
});

test("the implementer's own usage limit, before a run exists, also keeps the WIP", async () => {
  const env = wipEnv();
  env.next(env.session({ "b.txt": "b\n" }, undefined, LIMIT_TEXT)); // the session throws mid-way
  const err = await runTask(env.mk(), env.opts()).then(() => null, (e) => e);
  assert.ok(err instanceof UsageLimitError);
  assert.ok(err.run?.wip_sha);
  assert.ok(env.exists(err.run!.branch));
  assert.ok(!env.mainHasWip());
});

test("the usage limit without changes drops the branch and records no run", async () => {
  const env = wipEnv();
  env.next(env.session({}, LIMIT_TEXT));
  const err = await runTask(env.mk(), env.opts()).then(() => null, (e) => e);
  assert.ok(err instanceof UsageLimitError);
  assert.equal(err.run, undefined);
  assert.deepEqual(git(env.repo, "branch", "--list", "autonome/*"), "");
});

test("Try again applies the kept WIP in a new worktree and tells the session why it stopped", async () => {
  const env = wipEnv();
  env.next(env.session({ "b.txt": "b\n" }));
  const first = (await runTask(env.mk(), env.opts()))!;
  env.next(env.session({ "c.txt": "c\n" }));
  const again = (await runTask(env.mk([first]), env.opts()))!;
  const second = env.seen[1];
  assert.notEqual(second.cwd, first.worktree);
  assert.notEqual(again.branch, first.branch);
  assert.deepEqual(second.files, [".gitignore", "a.txt", "b.txt"]); // b.txt is there when the session starts
  assert.match(env.prompts[1], /A previous attempt stopped: lint failed: x\. Its work \(1 file changed, 1 insertion\(\+\)\) is applied in this worktree; check it before going on\./);
  assert.equal(git(env.repo, "rev-parse", again.branch), again.wip_sha); // the new WIP holds both files
  assert.deepEqual(git(env.repo, "show", "--name-only", "--format=", again.branch).split("\n").sort(), ["b.txt", "c.txt"]);
  assert.ok(!env.mainHasWip());
});

test("a WIP that does not apply on main starts clean and the prompt says where the branch is", async () => {
  const env = wipEnv();
  env.next(env.session({ "a.txt": "mine\n" }));
  const first = (await runTask(env.mk(), env.opts()))!;
  writeFileSync(join(env.repo, "a.txt"), "theirs\n");
  git(env.repo, "commit", "-qam", "main moved");
  env.next(env.session({}));
  await runTask(env.mk([first]), env.opts());
  assert.match(env.prompts[1], new RegExp(`A previous attempt stopped: lint failed: x\\. Its work is on branch ${first.branch} and did not apply on main; start from main\\.`));
  assert.equal(env.seen[1].status, ""); // clean at the start
  assert.equal(git(env.repo, "show", "main:a.txt"), "theirs");
  assert.ok(!env.mainHasWip());
});

test("a merge blocked after main moved keeps only the task's work for Try again", async () => {
  const env = wipEnv();
  const pass = { passed: true, summary: "all gates passed", outcomes: [] };
  let calls = 0;
  env.gates(() => (++calls === 1 ? pass : { passed: false, summary: "gates failed on top of main", outcomes: [] }));
  const work = env.session({ "b.txt": "b\n" });
  env.next(((args: any) => { // main gains a commit while the session runs, so the merge rebases
    writeFileSync(join(env.repo, "m.txt"), "m\n");
    git(env.repo, "add", "m.txt");
    git(env.repo, "commit", "-qm", "main moved");
    return work(args);
  }) as never);
  const first = (await runTask(env.mk(), env.opts()))!;
  assert.equal(first.outcome, "blocked");
  assert.match(first.reason!, /gates failed on top of main/);
  assert.equal(first.base, git(env.repo, "rev-parse", "main")); // where the branch leaves main, not the old base
  env.gates(() => ({ passed: false, summary: "lint failed: x", outcomes: [] }));
  env.next(env.session({}));
  await runTask(env.mk([first]), env.opts());
  assert.match(env.prompts[1], /Its work \(1 file changed, 1 insertion\(\+\)\) is applied/);
  assert.deepEqual(env.seen[1].files, [".gitignore", "a.txt", "b.txt", "m.txt"]);
});

test("a task without a WIP starts with the plain prompt", async () => {
  const env = wipEnv();
  await runTask(env.mk([{ at: "", branch: "x", worktree: "", base: "", outcome: "blocked", reason: "r" }]), env.opts());
  assert.equal(env.prompts[0], "Run task #5: t. Read the briefing, then implement it.");
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const passing = { passed: true, summary: "all gates passed", outcomes: [] };

/** The ff to main shows as main's sha moving; `order` has the rest. */
function mainSha(repo: string) { return git(repo, "rev-parse", "main"); }

test("the handoff is written while the gates run on top of main: the time is the longer of the two, not the sum", async () => {
  const env = wipEnv();
  env.next(env.session({ "b.txt": "b\n" }));
  let gateCalls = 0;
  env.gates(async () => { if (++gateCalls === 1) return passing; await sleep(800); return passing; });
  const opts = env.opts();
  opts.seams!.handoff = async () => { await sleep(800); };
  const t0 = Date.now();
  const run = (await runTask(env.mk(), opts))!;
  const ms = Date.now() - t0;
  assert.equal(run.outcome, "merged");
  assert.ok(ms < 1400, `took ${ms} ms; the gates and the handoff should overlap`);
});

test("main only moves after the handoff and the triage", async () => {
  const env = wipEnv();
  env.withTriage();
  env.next(env.session({ "b.txt": "b\n" }));
  env.gates(() => passing);
  const before = mainSha(env.repo);
  const opts = env.opts();
  opts.seams!.handoff = async () => {
    await sleep(100);
    env.order.push(`handoff (main at ${mainSha(env.repo) === before ? "base" : "moved"})`);
  };
  const run = (await runTask(env.mk(), opts))!;
  assert.equal(run.outcome, "merged");
  assert.deepEqual(env.order.slice(-2), ["handoff (main at base)", "triage"]);
  assert.notEqual(mainSha(env.repo), before);
});

test("a queue paused while the gates run merges nothing", async () => {
  const env = wipEnv();
  env.next(env.session({ "b.txt": "b\n" }));
  let gateCalls = 0;
  env.gates(() => { if (++gateCalls === 2) env.pause(); return passing; }); // the 2nd run is integrate's
  const before = mainSha(env.repo);
  const run = (await runTask(env.mk(), env.opts()))!;
  assert.equal(run.outcome, "paused");
  assert.equal(mainSha(env.repo), before);
});

test("gates failing on top of main block the task only after the handoff finished", async () => {
  const env = wipEnv();
  env.next(env.session({ "b.txt": "b\n" }));
  let gateCalls = 0;
  env.gates(() => (++gateCalls === 1 ? passing : { passed: false, summary: "gates failed on top of main", outcomes: [] }));
  let handoffDone = false;
  const opts = env.opts();
  opts.seams!.handoff = async () => { await sleep(150); handoffDone = true; };
  const before = mainSha(env.repo);
  const run = (await runTask(env.mk(), opts))!;
  assert.equal(run.outcome, "blocked");
  assert.match(run.reason!, /gates failed on top of main/);
  assert.ok(handoffDone);
  assert.equal(mainSha(env.repo), before);
});

test("the handoff gets the task's diff: the task's files and nothing from main", async () => {
  const env = wipEnv();
  const work = env.session({ "b.txt": "b\n" });
  env.next(((args: any) => { // main moves while the session runs, so the branch is rebased
    writeFileSync(join(env.repo, "m.txt"), "m\n");
    git(env.repo, "add", "m.txt");
    git(env.repo, "commit", "-qm", "main moved");
    return work(args);
  }) as never);
  env.gates(() => passing);
  const opts = env.opts();
  let seen: { base?: string; files?: string } = {};
  opts.seams!.handoff = async (_s, cwd, _t, _p, _l, base) => {
    seen = { base, files: git(cwd, "diff", "--name-only", `${base}..HEAD`) };
  };
  const run = (await runTask(env.mk(), opts))!;
  assert.equal(run.outcome, "merged");
  assert.equal(seen.files, "b.txt");
  assert.notEqual(seen.base, run.base);
});

test("a task that conflicts with main still gets its handoff, from where the branch leaves main", async () => {
  const env = wipEnv();
  env.withTriage();
  const work = env.session({ "a.txt": "mine\n" });
  env.next(((args: any) => { // main edits the same file while the session runs
    writeFileSync(join(env.repo, "a.txt"), "theirs\n");
    git(env.repo, "commit", "-qam", "main moved");
    return work(args);
  }) as never);
  env.gates(() => passing);
  const opts = env.opts();
  let handedOff = false;
  opts.seams!.handoff = async () => { handedOff = true; };
  const run = (await runTask(env.mk(), opts))!;
  assert.equal(run.outcome, "blocked");
  assert.match(run.reason!, /conflicts with main/);
  assert.ok(handedOff);
  assert.equal(env.order.at(-1), "triage");
});

test("a handoff that throws is only logged: the gates and the merge go on", async () => {
  const env = wipEnv();
  env.next(env.session({ "b.txt": "b\n" }));
  env.gates(() => passing);
  const opts = env.opts();
  const logs: string[] = [];
  opts.log = (l) => { logs.push(l); };
  opts.seams!.handoff = async () => { throw new Error("boom"); };
  const run = (await runTask(env.mk(), opts))!;
  assert.equal(run.outcome, "merged");
  assert.ok(logs.some((l) => l.includes("handoff failed: Error: boom")));
});

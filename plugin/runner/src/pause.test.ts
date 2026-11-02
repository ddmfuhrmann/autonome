import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, test } from "node:test";
import { ContextError, type Run } from "./context.js";
import { git } from "./git.js";
import { UsageLimitError } from "./limits.js";
import { RESUME_PROMPT, run } from "./run.js";
import { changes } from "./watch.js";

process.env.AUTONOME_CONTEXT_RETRY_MS = "1";
const realFetch = globalThis.fetch;
const realUrl = process.env.AUTONOME_CONTEXT_URL;
afterEach(() => {
  globalThis.fetch = realFetch;
  if (realUrl === undefined) delete process.env.AUTONOME_CONTEXT_URL; else process.env.AUTONOME_CONTEXT_URL = realUrl;
  delete process.env.AUTONOME_CONTEXT_WAIT_S;
});

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const CONFIG = { gates: [{ name: "t", run: "true" }], retries: { max: 1, stop_on_repeat: true },
  budget: { task_tokens: 1e6, task_minutes: 5, plan_tokens: 1e6 }, review: { enabled: false, max_rounds: 1 }, smoke_test: null };

function makeRepo() {
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-pause-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  writeFileSync(join(repo, "a.txt"), "a\n");
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "init");
  return repo;
}

const taskWith = (runs: Run[] = []) => ({ id: 9, project: "p", status: "running", feature: "links", layers: [],
  body: { title: "Links", spec: "s", acceptance: [], plan_id: 16, seq: 1, hold_before_merge: false, runs } });
const emptyClaim = { task: null, preflight: null, smoke_test: null };

interface Setup {
  claims: unknown[];
  /** Whether the project's queue is paused, asked on every read of its state. */
  paused?: () => boolean;
  stateFails?: boolean;
  /** The state read fails from this read on (1 = the first). */
  stateFailsFrom?: number;
  /** What the fake session does after its init message; gets the options the SDK would. */
  session?: (options: any) => void;
  watch?: number;
  gates?: () => { passed: boolean; summary: string };
  /** Turns the reviewer on, with this fake in its place. */
  review?: () => unknown;
  maxTasks?: number;
  /** How the fake session ends, as the result's subtype (default: success). */
  stop?: string;
}

/** Runs the real loop against a fake context and a fake Agent SDK. */
async function play(repo: string, s: Setup) {
  const transitions: { id: number; to: string; run?: Run }[] = [];
  const log: string[] = [];
  const queries: { prompt: string; options: any; interrupted: number }[] = [];
  let claims = 0;
  let stateReads = 0;
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/tasks/claim")) {
      claims++;
      // The run ends by spawning autonome-docs; keep that child off a real context on the default port.
      process.env.AUTONOME_CONTEXT_URL = "http://127.0.0.1:9";
      process.env.AUTONOME_CONTEXT_WAIT_S = "0";
      return json(s.claims.shift() ?? emptyClaim);
    }
    if (url.endsWith("/state") && ++stateReads) return s.stateFails || (s.stateFailsFrom && stateReads >= s.stateFailsFrom) ? new Response("down", { status: 500 }) : json({ paused: s.paused?.() ? { at: "", by: "jev", reason: "stop" } : null });
    if (url.endsWith("/gate-config")) return json({ config: { ...CONFIG, review: { enabled: !!s.review, max_rounds: 1 } } });
    if (url.includes("/briefing")) return json({ text: "briefing" });
    if (url.includes("/transition")) {
      transitions.push({ id: 9, ...JSON.parse(String(init?.body)) });
      return json({ ok: true });
    }
    if (url.endsWith("/waves/without-retro")) return json([]);
    return json({});
  };
  const query = ((args: { prompt: string; options: any }) => {
    const q = { prompt: args.prompt, options: args.options, interrupted: 0 };
    queries.push(q);
    const gen = (async function* () {
      yield { type: "system", subtype: "init", session_id: "s-new" };
      s.session?.(args.options);
      yield { type: "assistant", message: { usage: { input_tokens: 1, output_tokens: 1 }, content: [] } };
      yield { type: "result", subtype: q.interrupted ? "error_during_execution" : s.stop ?? "success", total_cost_usd: 0 };
    })();
    return Object.assign(gen, { interrupt: async () => { q.interrupted++; } });
  }) as never;
  const error = await run({ project: "p", repo, maxTasks: s.maxTasks ?? 5, push: false, watch: s.watch, log: (l) => log.push(l),
    seams: { query, gates: s.gates ? () => s.gates!() : undefined, review: s.review as never } }).then(() => undefined, (e: Error) => e);
  return { transitions, log, queries, claims, error };
}

test("a pause seen in the middle of the session interrupts it and keeps the session and the worktree", async () => {
  const repo = makeRepo();
  const { transitions, queries, error } = await play(repo, { claims: [{ task: taskWith(), preflight: null, smoke_test: null }], paused: () => true });
  assert.equal(error, undefined);
  assert.equal(queries[0].interrupted, 1);
  assert.deepEqual(transitions.map((t) => t.to), ["paused"]);
  const r = transitions[0].run!;
  assert.equal(r.outcome, "paused");
  assert.equal(r.session_id, "s-new");
  assert.ok(existsSync(r.worktree));
  assert.equal(git(repo, "rev-parse", "--verify", `refs/heads/${r.branch}`).length, 40); // the branch stays
  assert.equal(git(repo, "rev-parse", "main"), r.base);
});

test("a pause seen before the merge leaves main as it was and merges nothing", async () => {
  const repo = makeRepo();
  const main = git(repo, "rev-parse", "main");
  let paused = false;
  const { transitions } = await play(repo, {
    claims: [{ task: taskWith(), preflight: null, smoke_test: null }],
    paused: () => paused,
    session: (o) => writeFileSync(join(o.cwd, "b.txt"), "b\n"),
    gates: () => { paused = true; return { passed: true, summary: "all gates passed" }; }, // paused while the gates ran
  });
  assert.deepEqual(transitions.map((t) => t.to), ["paused"]);
  assert.equal(git(repo, "rev-parse", "main"), main);
  assert.ok(existsSync(join(transitions[0].run!.worktree, "b.txt"))); // the work stays uncommitted in the worktree
});

test("a pause that comes while the gates run on top of main stops before main moves", async () => {
  const repo = makeRepo();
  const main = git(repo, "rev-parse", "main");
  let paused = false;
  let gateRuns = 0;
  const { transitions } = await play(repo, {
    claims: [{ task: taskWith(), preflight: null, smoke_test: null }],
    paused: () => paused,
    session: (o) => writeFileSync(join(o.cwd, "b.txt"), "b\n"),
    gates: () => { if (++gateRuns === 2) paused = true; return { passed: true, summary: "all gates passed" }; }, // the 2nd run is integrate's
  });
  assert.equal(gateRuns, 2);
  assert.deepEqual(transitions.map((t) => t.to), ["paused"]);
  assert.equal(git(repo, "rev-parse", "main"), main);
  const wt = transitions[0].run!.worktree;
  assert.ok(existsSync(wt));
  assert.equal(git(wt, "status", "--porcelain"), "A  b.txt"); // the work is back as changes, for the resumed session
});

test("a pause that comes while the gates run keeps the reviewer from starting", async () => {
  const repo = makeRepo();
  let paused = false;
  let reviews = 0;
  const { transitions } = await play(repo, {
    claims: [{ task: taskWith(), preflight: null, smoke_test: null }],
    paused: () => paused,
    session: (o) => writeFileSync(join(o.cwd, "b.txt"), "b\n"),
    gates: () => { paused = true; return { passed: true, summary: "all gates passed" }; },
    review: () => { reviews++; return { result: { verdict: "pass", findings: [] }, cost: 0 }; },
  });
  assert.equal(reviews, 0);
  assert.deepEqual(transitions.map((t) => t.to), ["paused"]);
});

test("with --watch a paused queue waits and claims again", async () => {
  const repo = makeRepo();
  const { transitions, claims, log } = await play(repo, {
    claims: [{ ...emptyClaim, paused: { at: "", by: "jev", reason: "stop" } }, { task: taskWith(), preflight: null, smoke_test: null }],
    paused: () => true, watch: 0.01, maxTasks: 1 });
  assert.equal(claims, 2); // it waited and asked again
  assert.deepEqual(transitions.map((t) => t.to), ["paused"]); // the second claim's task was picked up
  assert.equal(log.filter((l) => l === "queue paused by jev: stop").length, 1);
});

test("a paused task with its worktree resumes the same session with the resume prompt", async () => {
  const repo = makeRepo();
  const branch = `autonome/task-9-${Date.now().toString(36)}`;
  const worktree = mkdtempSync(join(tmpdir(), "autonome-resume-wt-"));
  rmSync(worktree, { recursive: true });
  git(repo, "worktree", "add", "-q", "-b", branch, worktree, "main");
  const last: Run = { at: "", session_id: "s-old", branch, worktree, base: git(repo, "rev-parse", "main"), outcome: "paused" };
  const { queries, transitions } = await play(repo, {
    claims: [{ task: taskWith([last]), preflight: null, smoke_test: null }], paused: () => true });
  assert.equal(queries[0].options.resume, "s-old");
  assert.equal(queries[0].prompt, RESUME_PROMPT);
  assert.equal(queries[0].options.cwd, worktree);
  assert.equal(transitions[0].run!.worktree, worktree);
  assert.equal(transitions[0].run!.branch, branch);
});

test("a paused task whose worktree is gone starts a new session in a new worktree from its branch", async () => {
  const repo = makeRepo();
  const branch = "autonome/task-9-old";
  git(repo, "branch", branch, "main");
  const gone = join(dirname(repo), ".autonome-worktrees", "autonome-task-9-gone");
  const last: Run = { at: "", session_id: "s-old", branch, worktree: gone, base: git(repo, "rev-parse", "main"), outcome: "paused" };
  const { queries, log } = await play(repo, {
    claims: [{ task: taskWith([last]), preflight: null, smoke_test: null }], paused: () => true });
  assert.equal(queries[0].options.resume, undefined);
  assert.notEqual(queries[0].options.cwd, gone);
  assert.equal(git(queries[0].options.cwd, "rev-parse", "--abbrev-ref", "HEAD"), branch);
  assert.ok(log.some((l) => l.includes("is gone; new session in a new worktree from")));
});

test("a paused queue ends the run without taking a task", async () => {
  const repo = makeRepo();
  const { transitions, queries, claims, log, error } = await play(repo, {
    claims: [{ ...emptyClaim, paused: { at: "", by: "jev", reason: "undo #921" } }] });
  assert.equal(error, undefined);
  assert.equal(claims, 1);
  assert.equal(queries.length, 0);
  assert.deepEqual(transitions, []);
  assert.ok(log.includes("queue paused by jev: undo #921"));
});

test("watch says who paused the queue and which task stopped", () => {
  const state = (over: object) => ({ tasks: [{ id: 9, title: "t", status: "running" }], plans: [], waves: [], waiting: [], ...over });
  const lines = changes(state({}), state({ tasks: [{ id: 9, title: "t", status: "paused" }], paused: { at: "", by: "jev", reason: "undo #921" } }));
  assert.deepEqual(lines, ["info #9 paused", "needs-you queue paused by jev: undo #921"]);
});

test("the usage limit goes back to pending with its run, and the next claim starts from that WIP", async () => {
  const repo = makeRepo();
  const first = await play(repo, {
    claims: [{ task: taskWith(), preflight: null, smoke_test: null }],
    session: (o) => writeFileSync(join(o.cwd, "b.txt"), "b\n"),
    stop: "You've hit your session limit · resets 3:40pm" });
  assert.ok(first.error instanceof UsageLimitError);
  assert.deepEqual(first.transitions.map((t) => t.to), ["pending"]);
  const limited = first.transitions[0].run!;
  assert.equal(limited.outcome, "usage_limit");
  assert.ok(limited.wip_sha);
  assert.equal(git(repo, "rev-parse", limited.branch), limited.wip_sha);

  delete process.env.AUTONOME_CONTEXT_WAIT_S; // play sets these for one run; the next run waits for its context afresh
  delete process.env.AUTONOME_CONTEXT_URL;
  let files: string[] = [];
  const second = await play(repo, {
    claims: [{ task: { ...taskWith([limited]), status: "pending" }, preflight: null, smoke_test: null }],
    paused: () => true,
    session: (o) => { files = readdirSync(o.cwd); } });
  assert.ok(files.includes("b.txt")); // the WIP is applied when the session starts
  assert.match(second.queries[0].prompt, /A previous attempt stopped: .*session limit/);
  assert.ok(second.log.some((l) => l.includes("starts from the WIP of an earlier attempt")));
});

// Guards the behaviour, not the `outcome !== "paused"` check in runTask: that check is redundant today, because a
// paused run either reuses or re-adds its worktree (`fresh` is false) or has no branch (the rev-parse fails).
test("a paused run with a wip_sha does not start from the WIP", async () => {
  const repo = makeRepo();
  const branch = "autonome/task-9-old";
  git(repo, "branch", branch, "main");
  const base = git(repo, "rev-parse", "main");
  const last: Run = { at: "", session_id: "s-old", branch, worktree: join(dirname(repo), ".autonome-worktrees", "autonome-task-9-gone"),
    base, outcome: "paused", wip_sha: base };
  const { queries, log } = await play(repo, {
    claims: [{ task: taskWith([last]), preflight: null, smoke_test: null }], paused: () => true });
  assert.ok(!log.some((l) => l.includes("WIP of an earlier attempt")));
  assert.doesNotMatch(queries[0].prompt, /A previous attempt stopped/);
});

test("a context error in the pause lookup for the log still pauses the task with its session and worktree", async () => {
  const repo = makeRepo();
  const { transitions, error } = await play(repo, {
    claims: [{ task: taskWith(), preflight: null, smoke_test: null }], paused: () => true, stateFailsFrom: 2 });
  assert.equal(error, undefined);
  assert.deepEqual(transitions.map((t) => t.to), ["paused"]);
  const r = transitions[0].run!;
  assert.equal(r.outcome, "paused");
  assert.equal(r.session_id, "s-new");
  assert.ok(existsSync(r.worktree));
});

test("a context error while reading the pause hands the task back to pending, never pauses it", async () => {
  const repo = makeRepo();
  const { transitions, error } = await play(repo, { claims: [{ task: taskWith(), preflight: null, smoke_test: null }], stateFails: true });
  assert.ok(error instanceof ContextError);
  assert.deepEqual(transitions.map((t) => t.to), ["pending"]);
});

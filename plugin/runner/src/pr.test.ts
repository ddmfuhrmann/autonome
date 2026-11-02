import assert from "node:assert/strict";
import { existsSync, mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import { doctor } from "./doctor.js";
import { git, tryGit } from "./git.js";
import type { Run } from "./context.js";
import { openPr, prState, type Gh } from "./pr.js";
import { followPrs, resetPrFailures, run } from "./run.js";

process.env.AUTONOME_CONTEXT_RETRY_MS = "1";
const realFetch = globalThis.fetch;
const realUrl = process.env.AUTONOME_CONTEXT_URL;
afterEach(() => {
  globalThis.fetch = realFetch;
  if (realUrl === undefined) delete process.env.AUTONOME_CONTEXT_URL; else process.env.AUTONOME_CONTEXT_URL = realUrl;
  delete process.env.AUTONOME_CONTEXT_WAIT_S;
});

const PR = "https://github.com/acme/app/pull/42";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const CONFIG = { gates: [{ name: "t", run: "true" }], retries: { max: 1, stop_on_repeat: true },
  budget: { task_tokens: 1e6, task_minutes: 5, plan_tokens: 1e6 }, review: { enabled: false, max_rounds: 1 }, smoke_test: null };

/** A repo with a bare origin that already has main. */
function makeRepo() {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "autonome-pr-")));
  const origin = join(root, "origin.git");
  git(root, "init", "-q", "--bare", "-b", "main", origin);
  const repo = join(root, "repo");
  git(root, "init", "-q", "-b", "main", repo);
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  writeFileSync(join(repo, "a.txt"), "a\n");
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "init");
  git(repo, "remote", "add", "origin", origin);
  git(repo, "push", "-q", "origin", "main");
  return { repo, origin };
}

const task = { id: 9, project: "p", status: "running", feature: "links", layers: [],
  body: { title: "Links", spec: "s", acceptance: [], plan_id: 16, seq: 1, hold_before_merge: false, runs: [] as Run[] } };

/** A fake gh: records every call; `answer` can override what a call prints. */
function fakeGh(answer: (args: string[]) => { ok: boolean; out: string } | undefined = () => undefined) {
  const calls: string[][] = [];
  const gh: Gh = (args) => {
    calls.push(args);
    const given = answer(args);
    if (given) return given;
    if (args[0] === "pr" && args[1] === "create") return { ok: true, out: PR };
    return { ok: true, out: "" };
  };
  return { gh, calls };
}

interface Setup {
  merge?: { mode: "ff" | "pr"; pr: "manual" | "auto" };
  gh: Gh;
  paused?: () => boolean;
  /** Tasks the context reports in `in_pr`. */
  inPr?: { id: number; title: string; status: string; feature: string | null; pr_url: string }[];
  claims?: unknown[];
}

function fakeContext(s: Setup) {
  const transitions: { id: number; to: string; run?: Run }[] = [];
  const claims = [...(s.claims ?? [])];
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/tasks/claim")) {
      // The run ends by spawning autonome-docs; keep that child off a real context on the default port.
      process.env.AUTONOME_CONTEXT_URL = "http://127.0.0.1:9";
      process.env.AUTONOME_CONTEXT_WAIT_S = "0";
      return json(claims.shift() ?? { task: null, preflight: null, smoke_test: null });
    }
    if (url.endsWith("/state")) return json({ paused: s.paused?.() ? { at: "", by: "jev", reason: "stop" } : null, tasks: s.inPr ?? [] });
    if (url.endsWith("/gate-config")) return json({ config: { ...CONFIG, ...(s.merge ? { merge: s.merge } : {}) } });
    if (url.includes("/briefing")) return json({ text: "briefing" });
    if (url.includes("/transition")) {
      transitions.push({ id: Number(/tasks\/(\d+)\//.exec(url)?.[1]), ...JSON.parse(String(init?.body)) });
      return json({ ok: true });
    }
    if (url.endsWith("/waves/without-retro")) return json([]);
    return json({});
  };
  return transitions;
}

/** Runs the real loop against a fake context, a fake Agent SDK and a fake gh. */
async function play(repo: string, s: Setup, session: (cwd: string) => void = (cwd) => writeFileSync(join(cwd, "b.txt"), "b\n")) {
  const transitions = fakeContext({ ...s, claims: s.claims ?? [{ task, preflight: null, smoke_test: null }] });
  const log: string[] = [];
  const query = ((args: { options: { cwd: string } }) => {
    const gen = (async function* () {
      yield { type: "system", subtype: "init", session_id: "s-new" };
      session(args.options.cwd);
      yield { type: "assistant", message: { usage: { input_tokens: 1, output_tokens: 1 }, content: [] } };
      yield { type: "result", subtype: "success", total_cost_usd: 0 };
    })();
    return Object.assign(gen, { interrupt: async () => {} });
  }) as never;
  const error = await run({ project: "p", repo, maxTasks: 1, push: false, log: (l) => log.push(l),
    seams: { query, gh: s.gh, docs: () => {}, gates: () => ({ passed: true, summary: "all gates passed" }) } }).then(() => undefined, (e: Error) => e);
  return { transitions, log, error };
}

const PR_MANUAL = { mode: "pr", pr: "manual" } as const;

test("pr mode, manual: pushes the branch, opens the pull request from it and moves the task to in_pr with its url", async () => {
  const { repo, origin } = makeRepo();
  const main = git(repo, "rev-parse", "main");
  const { gh, calls } = fakeGh();
  const { transitions, error } = await play(repo, { merge: PR_MANUAL, gh });
  assert.equal(error, undefined);
  assert.deepEqual(transitions.map((t) => t.to), ["in_pr"]);
  const r = transitions[0].run!;
  assert.equal(r.pr_url, PR);
  assert.equal(r.pr_mode, "manual");
  const create = calls.find((c) => c[0] === "pr" && c[1] === "create")!;
  assert.equal(create[create.indexOf("--head") + 1], r.branch);
  assert.equal(create[create.indexOf("--base") + 1], "main");
  assert.match(create[create.indexOf("--title") + 1], /^feat\(links\): links$/);
  assert.match(create[create.indexOf("--body") + 1], /Task #9/);
  assert.ok(!calls.some((c) => c[1] === "merge"), "manual: nobody asks GitHub to merge");
  assert.equal(git(origin, "rev-parse", r.branch), git(repo, "rev-parse", r.branch)); // pushed
  assert.equal(git(repo, "rev-parse", "main"), main); // main did not move
  assert.ok(!existsSync(r.worktree), "the worktree is removed");
  assert.ok(tryGit(repo, "rev-parse", "--verify", `refs/heads/${r.branch}`).ok, "the branch stays");
});

test("pr mode, auto: asks GitHub to merge the pull request when CI passes", async () => {
  const { repo } = makeRepo();
  const { gh, calls } = fakeGh();
  const { transitions } = await play(repo, { merge: { mode: "pr", pr: "auto" }, gh });
  assert.deepEqual(calls.find((c) => c[1] === "merge"), ["pr", "merge", "--auto", "--rebase", PR]);
  assert.equal(transitions[0].to, "in_pr");
  assert.equal(transitions[0].run!.pr_mode, "auto");
});

test("pr mode, auto: a refused auto-merge leaves the pull request to the human instead of failing", async () => {
  const { repo } = makeRepo();
  const { gh } = fakeGh((a) => (a[1] === "merge" ? { ok: false, out: "auto merge is not allowed" } : undefined));
  const { transitions, log } = await play(repo, { merge: { mode: "pr", pr: "auto" }, gh });
  assert.equal(transitions[0].to, "in_pr");
  assert.equal(transitions[0].run!.pr_mode, "manual");
  assert.ok(log.some((l) => l.includes("could not turn on auto-merge")));
});

test("gh failing on pr create hands the task back to pending (never blocked) and logs why", async () => {
  const { repo } = makeRepo();
  const { gh } = fakeGh((a) => (a[1] === "create" ? { ok: false, out: "HTTP 502: bad gateway" } : undefined));
  const { transitions, log, error } = await play(repo, { merge: PR_MANUAL, gh });
  assert.equal(error, undefined);
  assert.deepEqual(transitions.map((t) => t.to), ["pending"]);
  const r = transitions[0].run!;
  assert.equal(r.outcome, "error");
  assert.match(r.reason!, /gh pr create failed: HTTP 502/);
  assert.ok(r.wip_sha, "the work is kept on the branch");
  assert.ok(log.some((l) => l.includes("warning:") && l.includes("goes back to pending")));
});

test("a failing push is infrastructure too: pending, no pull request", async () => {
  const { repo, origin } = makeRepo();
  git(repo, "remote", "set-url", "origin", join(origin, "nowhere"));
  const { gh, calls } = fakeGh();
  const { transitions } = await play(repo, { merge: PR_MANUAL, gh });
  assert.deepEqual(transitions.map((t) => t.to), ["pending"]);
  assert.ok(!calls.some((c) => c[1] === "create"));
});

test("with the queue paused nothing is pushed and no pull request is opened", async () => {
  const { repo, origin } = makeRepo();
  let paused = false;
  const { gh, calls } = fakeGh();
  const { transitions } = await play(repo, { merge: PR_MANUAL, gh, paused: () => paused },
    (cwd) => { writeFileSync(join(cwd, "b.txt"), "b\n"); paused = true; });
  assert.deepEqual(transitions.map((t) => t.to), ["paused"]);
  assert.ok(!calls.some((c) => c[0] === "pr"));
  assert.equal(git(origin, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/main"); // nothing but main is there
});

test("a task released from the hold, with the queue paused, pushes nothing and calls no gh", async () => {
  const { repo, origin } = makeRepo();
  const branch = "autonome/task-9-held";
  const worktree = join(repo, "..", "held-wt");
  git(repo, "worktree", "add", "-q", "-b", branch, worktree, "main");
  writeFileSync(join(worktree, "b.txt"), "b\n");
  git(worktree, "add", ".");
  git(worktree, "commit", "-qm", "feat: b");
  const last: Run = { at: "", branch, worktree, base: git(repo, "rev-parse", "main"), outcome: "on_hold" };
  const { gh, calls } = fakeGh();
  const { transitions } = await play(repo, { merge: PR_MANUAL, gh, paused: () => true,
    claims: [{ task: { ...task, status: "released", body: { ...task.body, runs: [last] } }, preflight: null, smoke_test: null }] });
  assert.deepEqual(transitions, []);
  assert.deepEqual(calls.filter((c) => c[0] === "pr"), []);
  assert.equal(git(origin, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/main");
});

test("fast-forward mode is unchanged: main moves, gh is never called", async () => {
  const { repo } = makeRepo();
  const { gh, calls } = fakeGh();
  const { transitions } = await play(repo, { gh });
  assert.deepEqual(transitions.map((t) => [t.to, t.run?.reason]), [["done", undefined]]);
  assert.ok(transitions[0].run!.merged_sha);
  assert.equal(git(repo, "rev-parse", "main"), transitions[0].run!.merged_sha);
  assert.deepEqual(calls, []);
});

const waiting = [{ id: 9, title: "Links", status: "in_pr", feature: "links", pr_url: PR }];

async function follow(repo: string, gh: Gh) {
  const transitions = fakeContext({ merge: PR_MANUAL, gh, inPr: waiting });
  const log: string[] = [];
  const docs: string[][] = [];
  await followPrs({ project: "p", repo, maxTasks: 1, push: false, log: (l) => log.push(l), seams: { gh, docs: (_r, _p, a) => docs.push(a) } });
  return { transitions, log, docs };
}

const view = (state: string, oid?: string) => (a: string[]) =>
  a[1] === "view" ? { ok: true, out: JSON.stringify({ state, mergeCommit: oid ? { oid } : null }) } : undefined;

test("a MERGED pull request makes the task done with the merge commit, and main catches up", async () => {
  const { repo, origin } = makeRepo();
  const other = join(repo, "..", "other");
  git(repo, "clone", "-q", origin, other);
  git(other, "config", "user.email", "t@t");
  git(other, "config", "user.name", "t");
  writeFileSync(join(other, "c.txt"), "c\n");
  git(other, "add", ".");
  git(other, "commit", "-qm", "merged on GitHub");
  git(other, "push", "-q", "origin", "main");
  const sha = git(other, "rev-parse", "HEAD");
  const { gh, calls } = fakeGh(view("MERGED", sha));
  const { transitions, docs } = await follow(repo, gh);
  assert.deepEqual(calls[0], ["pr", "view", PR, "--json", "state,mergeCommit"]);
  assert.deepEqual(transitions.map((t) => [t.id, t.to, t.run?.merged_sha]), [[9, "done", sha]]);
  assert.equal(git(repo, "rev-parse", "main"), sha);
  assert.deepEqual(docs, [["--feature", "links"]]);
});

test("a CLOSED pull request blocks the task", async () => {
  const { repo } = makeRepo();
  const { transitions } = await follow(repo, fakeGh(view("CLOSED")).gh);
  assert.deepEqual(transitions.map((t) => [t.to, t.run?.reason]), [["blocked", "PR closed without merge"]]);
});

test("an OPEN pull request changes nothing", async () => {
  const { repo } = makeRepo();
  const { transitions } = await follow(repo, fakeGh(view("OPEN")).gh);
  assert.deepEqual(transitions, []);
});

test("gh failing while following a pull request warns twice, then blocks the task on the third time in a row", async () => {
  resetPrFailures();
  const { repo } = makeRepo();
  const broken = fakeGh((a) => (a[1] === "view" ? { ok: false, out: "gh is not installed" } : undefined)).gh;
  const first = await follow(repo, broken);
  assert.deepEqual(first.transitions, []);
  assert.ok(first.log.some((l) => l.includes("warning: #9") && l.includes("gh is not installed")));
  assert.deepEqual((await follow(repo, broken)).transitions, []);
  const third = await follow(repo, broken);
  assert.deepEqual(third.transitions.map((t) => [t.id, t.to]), [[9, "blocked"]]);
  assert.match(third.transitions[0].run!.reason!, /could not follow the pull request.*gh is not installed/);
});

test("a pull request seen again resets the failures in a row", async () => {
  resetPrFailures();
  const { repo } = makeRepo();
  const broken = fakeGh((a) => (a[1] === "view" ? { ok: false, out: "no network" } : undefined)).gh;
  await follow(repo, broken);
  await follow(repo, broken);
  await follow(repo, fakeGh(view("OPEN")).gh);
  const { transitions } = await follow(repo, broken);
  assert.deepEqual(transitions, []);
});

test("in fast-forward mode the runner does not look at pull requests", async () => {
  const { repo } = makeRepo();
  const { gh, calls } = fakeGh(view("MERGED", "abc"));
  const transitions = fakeContext({ gh, inPr: waiting });
  await followPrs({ project: "p", repo, maxTasks: 1, push: false, log: () => {}, seams: { gh } });
  assert.deepEqual([transitions, calls], [[], []]);
});

test("doctor with merge.mode pr fails the gh check when gh is not logged in, and passes when it is", async () => {
  const { repo } = makeRepo();
  fakeContext({ merge: PR_MANUAL, gh: fakeGh().gh });
  const bad = await doctor("p", repo, { gh: () => ({ ok: false, out: "You are not logged into any GitHub hosts." }) });
  const check = bad.find((c) => c.name === "gh")!;
  assert.equal(check.ok, false);
  assert.match(check.detail, /not logged into any GitHub hosts.*gh auth login/);
  const good = await doctor("p", repo, { gh: () => ({ ok: true, out: "Logged in" }) });
  assert.equal(good.find((c) => c.name === "gh")!.ok, true);
});

test("doctor does not ask for gh in fast-forward mode", async () => {
  const { repo } = makeRepo();
  fakeContext({ gh: fakeGh().gh });
  const checks = await doctor("p", repo, { gh: () => { throw new Error("gh must not be called"); } });
  assert.ok(!checks.some((c) => c.name === "gh"));
});

test("openPr and prState read what gh prints", () => {
  const { gh } = fakeGh((a) => (a[1] === "create" ? { ok: true, out: `Creating pull request\n${PR}\n` } : undefined));
  assert.deepEqual(openPr(gh, "/", { branch: "b", title: "t", body: "x", mode: "manual" }), { url: PR, mode: "manual" });
  assert.throws(() => prState(() => ({ ok: true, out: "not json" }), "/", PR), /unexpected/);
});

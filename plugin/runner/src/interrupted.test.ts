// The runner stopped by hand (gastos and autonome, 2026-11-03): two Ctrl-Cs left two sessions running on
// their own, their tasks `running` in the context, and the next run took another task past them.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

const home = mkdtempSync(join(tmpdir(), "autonome-home-"));
process.env.AUTONOME_HOME = home;
process.env.AUTONOME_CONTEXT_WAIT_S = "2";
const { recoverInterrupted, runExclusive, taskWorktrees, INTERRUPTED } = await import("./run.js");
const { acquireRunLock, lockPath, RunLockedError } = await import("./runlock.js");
const { killLiveGroups, trackGroup } = await import("./processes.js");
const { git } = await import("./git.js");
type Run = import("./context.js").Run;

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });

/** A fake context with these tasks `running`; records the transitions. */
function fakeContext(running: { id: number; title: string }[]) {
  const transitions: { id: number; to: string; run?: Run }[] = [];
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/state")) return json({ paused: null, tasks: running.map((t) => ({ ...t, status: "running" })) });
    const m = url.match(/\/api\/tasks\/(\d+)\/transition$/);
    if (m) {
      const body = JSON.parse(String(init?.body)) as { to: string; run?: Run };
      transitions.push({ id: Number(m[1]), to: body.to, run: body.run });
      return json({ status: body.to });
    }
    return new Response("not found", { status: 404 });
  };
  return transitions;
}

function repoWithTask(taskId: number): { repo: string; worktree: string; branch: string } {
  const root = mkdtempSync(join(tmpdir(), "interrupted-"));
  const repo = join(root, "repo");
  mkdirSync(repo);
  git(repo, "init", "-q", "-b", "main");
  writeFileSync(join(repo, "a.txt"), "a\n");
  git(repo, "add", ".");
  git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init");
  const branch = `autonome/task-${taskId}-${Date.now().toString(36)}`;
  const worktree = join(root, ".autonome-worktrees", branch.replaceAll("/", "-"));
  git(repo, "worktree", "add", "-q", "-b", branch, worktree, "main");
  return { repo, worktree, branch };
}

const opts = (repo: string, log: string[] = []) => ({ project: "p", repo, maxTasks: 1, push: false, log: (l: string) => log.push(l) });

test("a task left running with work in its worktree goes back to pending with that work as WIP", async () => {
  const { repo, worktree, branch } = repoWithTask(7);
  writeFileSync(join(worktree, "a.txt"), "changed\n");
  writeFileSync(join(worktree, "new.txt"), "new\n");
  const transitions = fakeContext([{ id: 7, title: "t" }]);
  const log: string[] = [];
  await recoverInterrupted(opts(repo, log));
  assert.equal(transitions.length, 1);
  const [t] = transitions;
  assert.equal(t.to, "pending");
  assert.equal(t.run?.outcome, "interrupted");
  assert.equal(t.run?.reason, INTERRUPTED);
  assert.equal(t.run?.branch, branch);
  assert.equal(t.run?.base, git(repo, "rev-parse", "main"));
  assert.equal(git(repo, "rev-parse", branch), t.run?.wip_sha); // the branch stays, with the WIP on it
  assert.match(git(repo, "show", "--stat", "--format=%s", branch), /wip\(task-7\)[\s\S]*new\.txt/);
  assert.ok(!existsSync(worktree));
  assert.match(log.join("\n"), /#7 .*back to pending, its work kept as WIP/);
});

test("a task left running with a clean worktree goes back to pending with no run, and its branch goes", async () => {
  const { repo, worktree, branch } = repoWithTask(8);
  const transitions = fakeContext([{ id: 8, title: "t" }]);
  await recoverInterrupted(opts(repo));
  assert.deepEqual(transitions, [{ id: 8, to: "pending", run: undefined }]);
  assert.ok(!existsSync(worktree));
  assert.throws(() => git(repo, "rev-parse", "--verify", `refs/heads/${branch}`));
});

test("a task left running with no worktree goes back to pending", async () => {
  const { repo } = repoWithTask(9);
  const transitions = fakeContext([{ id: 3, title: "t" }]);
  await recoverInterrupted(opts(repo));
  assert.deepEqual(transitions, [{ id: 3, to: "pending", run: undefined }]);
});

test("a WIP commit that fails keeps the worktree and still hands the task back", async () => {
  const { repo, worktree } = repoWithTask(10);
  writeFileSync(join(worktree, "a.txt"), "changed\n");
  const transitions = fakeContext([{ id: 10, title: "t" }]);
  await recoverInterrupted({ ...opts(repo), seams: { commitWip: () => { throw new Error("disk full"); } } });
  assert.deepEqual(transitions.map((t) => [t.id, t.to, t.run]), [[10, "pending", undefined]]);
  assert.ok(existsSync(worktree));
});

test("taskWorktrees finds only that task's branches, newest last", () => {
  const { repo, branch } = repoWithTask(11);
  const later = `autonome/task-11-${(Date.now() + 1000).toString(36)}`;
  git(repo, "worktree", "add", "-q", "-b", later, join(repo, "..", "later"), "main");
  git(repo, "worktree", "add", "-q", "-b", "autonome/task-111-zz", join(repo, "..", "other"), "main");
  assert.deepEqual(taskWorktrees(repo, 11).map((w) => w.branch), [branch, later]);
});

test("the run lock: a second runner on the same project is refused while the first is alive", () => {
  const dir = mkdtempSync(join(tmpdir(), "lock-"));
  const release = acquireRunLock(dir, "p");
  assert.throws(() => acquireRunLock(dir, "p", process.pid + 1), RunLockedError);
  const other = acquireRunLock(dir, "q"); // another project is not held
  other();
  release();
  acquireRunLock(dir, "p")(); // free again once released
});

test("the run lock left by a runner that died is taken over, and a half-written one too", () => {
  const dir = mkdtempSync(join(tmpdir(), "lock-"));
  mkdirSync(join(dir, "state"), { recursive: true });
  writeFileSync(lockPath(dir, "p"), JSON.stringify({ pid: 2 ** 22 + 12345, at: "" })); // no such process
  acquireRunLock(dir, "p")();
  writeFileSync(lockPath(dir, "p"), "{");
  acquireRunLock(dir, "p")();
});

test("release only removes the lock this process holds", () => {
  const dir = mkdtempSync(join(tmpdir(), "lock-"));
  const release = acquireRunLock(dir, "p");
  writeFileSync(lockPath(dir, "p"), JSON.stringify({ pid: process.pid + 1 })); // taken over meanwhile
  release();
  assert.ok(existsSync(lockPath(dir, "p")));
});

test("runExclusive refuses to start while another runner holds the project, and runs nothing", async () => {
  const dir = mkdtempSync(join(tmpdir(), "lock-"));
  const { repo } = repoWithTask(12);
  fakeContext([]);
  const held = acquireRunLock(dir, "p", process.ppid); // the parent process is alive
  let ran = false;
  await assert.rejects(runExclusive(opts(repo), dir, async () => { ran = true; return 0; }), RunLockedError);
  assert.equal(ran, false);
  held();
});

test("runExclusive puts interrupted tasks back before running, then releases the lock", async () => {
  const dir = mkdtempSync(join(tmpdir(), "lock-"));
  const { repo, worktree } = repoWithTask(13);
  writeFileSync(join(worktree, "a.txt"), "changed\n");
  const transitions = fakeContext([{ id: 13, title: "t" }]);
  let seen = 0;
  await runExclusive(opts(repo), dir, async () => { seen = transitions.length; return 0; });
  assert.equal(seen, 1);
  assert.ok(!existsSync(lockPath(dir, "p")));
});

test("killLiveGroups kills a detached group that Ctrl-C would not reach", async () => {
  const child = spawn("sh", ["-c", "sleep 30 & sleep 30"], { detached: true, stdio: "ignore" });
  trackGroup(child.pid);
  const exited = new Promise((r) => child.on("exit", r));
  assert.equal(await killLiveGroups(500), 1);
  await exited;
  assert.throws(() => process.kill(-child.pid!, 0)); // the whole group is gone
});

import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { allowedTools } from "./conduct.js";
import { commitMessage, fastForwardMain, git } from "./git.js";

function repo(): string {
  const dir = mkdtempSync(join(tmpdir(), "autonome-runner-"));
  git(dir, "init", "-q", "-b", "main");
  git(dir, "config", "user.email", "t@t");
  git(dir, "config", "user.name", "t");
  writeFileSync(join(dir, "a.txt"), "a\n");
  git(dir, "add", ".");
  git(dir, "commit", "-qm", "init");
  return dir;
}

function branchWithCommit(dir: string, name: string): void {
  const wt = join(dir, "..", `${name}-wt-${Date.now()}`);
  git(dir, "worktree", "add", "-q", "-b", name, wt, "main");
  writeFileSync(join(wt, "b.txt"), "b\n");
  git(wt, "add", ".");
  git(wt, "commit", "-qm", "b");
}

test("commit message follows conventional commits with plan and task trailers", () => {
  const msg = commitMessage({ id: 7, feature: "wallet", body: { title: "Fix: Rounding of fees", plan_id: 3 } });
  assert.match(msg, /^fix\(wallet\): rounding of fees\n\nPlan: #3\nTask: #7\n/);
  assert.match(commitMessage({ id: 1, feature: null, body: { title: "Add signup form", plan_id: 1 } }), /^feat: add signup form/);
});

test("main is fast-forwarded in place when its checkout is clean", () => {
  const dir = repo();
  branchWithCommit(dir, "task");
  assert.deepEqual(fastForwardMain(dir, "task"), { ok: true });
  assert.equal(git(dir, "rev-parse", "main"), git(dir, "rev-parse", "task"));
  assert.equal(git(dir, "status", "--porcelain"), "");
});

test("uncommitted work on the main checkout is never touched", () => {
  const dir = repo();
  branchWithCommit(dir, "task");
  writeFileSync(join(dir, "a.txt"), "local edit\n");
  const r = fastForwardMain(dir, "task");
  assert.equal(r.ok, false);
  assert.match(r.reason!, /uncommitted changes/);
});

test("main is updated by ref when it is not checked out anywhere", () => {
  const dir = repo();
  git(dir, "checkout", "-q", "-b", "other");
  branchWithCommit(dir, "task");
  assert.deepEqual(fastForwardMain(dir, "task"), { ok: true });
  assert.equal(git(dir, "rev-parse", "main"), git(dir, "rev-parse", "task"));
});

test("a musician may run the gates' commands and nothing else in the shell", () => {
  const tools = allowedTools({
    gates: [{ name: "lint", run: "./mvnw -q checkstyle:check" }, { name: "neg", run: "! grep -q TODO x" }],
    allowed_commands: ["pnpm"],
    encore: { max: 3, stop_on_repeat: true },
    budget: { task_tokens: 1, task_minutes: 1, plan_tokens: 1 },
  });
  assert.ok(tools.includes("Bash(./mvnw:*)") && tools.includes("Bash(pnpm:*)") && tools.includes("Bash(autonome-gates:*)"));
  assert.ok(!tools.some((t) => t === "Bash" || t.startsWith("Bash(git commit") || t.startsWith("Bash(git push")));
});

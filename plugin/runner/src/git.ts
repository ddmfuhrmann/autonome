// Git plumbing for the runner: one worktree per task, rebase onto main, fast-forward main.
import { execFileSync } from "node:child_process";

export function git(cwd: string, ...args: string[]): string {
  return execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}

export function tryGit(cwd: string, ...args: string[]): { ok: boolean; out: string } {
  try {
    return { ok: true, out: git(cwd, ...args) };
  } catch (e) {
    const err = e as { stdout?: string; stderr?: string };
    return { ok: false, out: `${err.stdout ?? ""}${err.stderr ?? ""}`.trim() };
  }
}

/** Where `main` is checked out, if anywhere. */
export function mainCheckout(repo: string, main = "main"): string | null {
  const list = git(repo, "worktree", "list", "--porcelain").split("\n\n");
  for (const entry of list) {
    const path = entry.match(/^worktree (.+)$/m)?.[1];
    if (path && entry.includes(`branch refs/heads/${main}`)) return path;
  }
  return null;
}

/**
 * Moves `main` to `branch` (fast-forward only) without ever touching uncommitted work:
 * merges in place where main is checked out and clean, updates the ref where it is not
 * checked out, and refuses otherwise.
 */
export function fastForwardMain(repo: string, branch: string, main = "main"): { ok: boolean; reason?: string } {
  const where = mainCheckout(repo, main);
  if (where) {
    if (git(where, "status", "--porcelain")) return { ok: false, reason: `${main} is checked out with uncommitted changes at ${where}` };
    const r = tryGit(where, "merge", "--ff-only", branch);
    return r.ok ? { ok: true } : { ok: false, reason: r.out };
  }
  const r = tryGit(repo, "fetch", ".", `${branch}:${main}`);
  return r.ok ? { ok: true } : { ok: false, reason: r.out };
}

export function commitMessage(task: { id: number; feature: string | null; body: { title: string; plan_id: number } }): string {
  const title = task.body.title.trim();
  const type = /^(fix|refactor|test|docs|build|ci|chore|perf)\b/i.exec(title)?.[1]?.toLowerCase() ?? "feat";
  const summary = title.replace(/^(fix|refactor|test|docs|build|ci|chore|perf)\b[:\s-]*/i, "");
  const subject = `${type}${task.feature ? `(${task.feature})` : ""}: ${summary.charAt(0).toLowerCase()}${summary.slice(1)}`;
  return `${subject}\n\nPlan: #${task.body.plan_id}\nTask: #${task.id}\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n`;
}

/**
 * Stages the work for the commit, recomputed from scratch each time: a file staged in an earlier
 * round and ignored since (a coverage file, say) drops out instead of staying tracked forever.
 */
export function stageWork(worktree: string): void {
  git(worktree, "reset", "-q");
  git(worktree, "add", "-A");
}

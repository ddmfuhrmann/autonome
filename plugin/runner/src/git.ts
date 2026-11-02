// Git plumbing for the runner: one worktree per task, rebase onto main, fast-forward main.
import { execFileSync } from "node:child_process";
import { sessionEnv } from "./secrets.js";

export function git(cwd: string, ...args: string[]): string {
  // Git runs the repo's hooks: they get no Jev key.
  return execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], env: sessionEnv() }).trim();
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
  const stripped = title.replace(/^(fix|refactor|test|docs|build|ci|chore|perf)\b[:\s-]*/i, "");
  // "Docs, README and version": without the type word it would start with punctuation, so keep it whole.
  const summary = /^[\p{L}\p{N}]/u.test(stripped) ? stripped : title;
  // "Count clicks" -> "count clicks", but an acronym or a name keeps its case: "POST", "SQLite".
  const firstWord = summary.split(/\s/)[0];
  const lower = firstWord.slice(1) === firstWord.slice(1).toLowerCase();
  const subject = `${type}${task.feature ? `(${task.feature})` : ""}: ${lower ? summary.charAt(0).toLowerCase() : summary.charAt(0)}${summary.slice(1)}`;
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

/**
 * Commits whatever the worktree holds (what .gitignore does not exclude) as WIP on its branch, before
 * the worktree is removed. Returns the commit, or null when there is nothing to keep. Throws when the
 * commit fails, so the caller keeps the worktree.
 */
export function commitWip(worktree: string, message: string): string | null {
  stageWork(worktree);
  if (!git(worktree, "status", "--porcelain")) return null;
  git(worktree, "-c", "user.name=autonome", "-c", "user.email=autonome@localhost", "commit", "-q", "--no-verify", "-m", message);
  return git(worktree, "rev-parse", "HEAD");
}

/** The subject of a WIP commit: the first line of why the task stopped, cut to 72 characters. */
export function wipMessage(taskId: number, reason: string): string {
  return `wip(task-${taskId}): ${reason.split("\n")[0].trim().slice(0, 72)}`;
}

/**
 * Applies an earlier attempt's work (the diff from `base` to its WIP commit) in a fresh worktree.
 * When it does not apply, the worktree is left as it was (clean) and `ok` is false.
 */
export function applyWip(worktree: string, base: string, wip: string): { ok: boolean; stat: string } {
  const env = sessionEnv();
  const run = (args: string[], input?: string) =>
    execFileSync("git", args, { cwd: worktree, encoding: "utf8", input, env, maxBuffer: 512 * 1024 * 1024, stdio: ["pipe", "pipe", "pipe"] });
  try {
    const patch = run(["diff", "--binary", base, wip]);
    if (patch) run(["apply", "--3way"], patch);
    return { ok: true, stat: run(["diff", "--shortstat", base, wip]).trim() || "no changes" };
  } catch {
    tryGit(worktree, "reset", "-q", "--hard");
    tryGit(worktree, "clean", "-fdq");
    return { ok: false, stat: "" };
  }
}

/**
 * Staged files that do not belong in a task's change: ones .gitignore excludes but are tracked
 * anyway, and new binary files the task (its title and spec) does not mention, build wrappers aside.
 */
// Binaries a build tool commits on purpose: its wrapper, which `./gradlew` or `./mvnw` needs to run.
const BUILD_WRAPPERS = ["gradle/wrapper/gradle-wrapper.jar", ".mvn/wrapper/maven-wrapper.jar"];

export function strayFiles(worktree: string, base: string, taskText: string): string[] {
  const changed = new Set(git(worktree, "diff", "--cached", "--name-only", base).split("\n").filter(Boolean));
  const ignored = git(worktree, "ls-files", "-ci", "--exclude-standard").split("\n")
    .filter((f) => changed.has(f)).map((f) => `${f}: tracked although .gitignore excludes it`);
  const binaries = git(worktree, "diff", "--cached", "--numstat", "--diff-filter=A", base).split("\n")
    .filter((l) => l.startsWith("-\t-\t")).map((l) => l.slice(4))
    .filter((f) => !BUILD_WRAPPERS.some((w) => f === w || f.endsWith(`/${w}`)))
    .filter((f) => !taskText.includes(f) && !taskText.includes(f.split("/").at(-1)!))
    .map((f) => `${f}: a new binary file the task does not mention`);
  return [...ignored, ...binaries];
}

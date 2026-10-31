// The maestro: takes approved tasks one by one, plays each in its own session and worktree, and
// only lets work reach main when the gates agree.
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { context, type GateConfig, type Run, type Task } from "./context.js";
import { commitMessage, fastForwardMain, git, tryGit } from "./git.js";

const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const AUTONOME_HOME = process.env.AUTONOME_HOME ?? join(homedir(), ".autonome");

export interface ConductOptions {
  project: string;
  repo: string;
  model?: string;
  maxTasks: number;
  push: boolean;
  log: (line: string) => void;
}

const MUSICIAN_PROMPT = `You are the implementer of one task in the autonome process. The briefing injected at
the start of this session has the task, its plan, the conventions for its layers and the gates.

- Do exactly the task. Nothing listed as out of scope, no drive-by refactors.
- Prove each acceptance criterion with a test or a gate where possible.
- While working, run the fast gates with \`autonome-gates --fast\`.
- Do not commit, push, switch branches or touch git history: the runner does that.
- Never edit, weaken or skip a gate or the rules behind it to make it pass.
- Nobody will answer questions. If something is truly ambiguous, choose the option most consistent
  with the plan and the conventions, and say so in your final message.
- When done, end your turn with a short summary. The metronome then runs every gate and may send you
  back with a failure (an encore): fix the cause, not the symptom.`;

/**
 * What a musician may do without asking (nobody is there to ask): read and edit files in its
 * worktree, run the project's gate commands and the commands the gate config allows, and read git.
 * Everything else is denied.
 */
export function allowedTools(cfg: GateConfig["config"]): string[] {
  const commands = new Set<string>(["autonome-gates", ...(cfg.allowed_commands ?? [])]);
  for (const g of cfg.gates) {
    const first = g.run.trim().split(/\s+/)[0];
    if (first && !first.startsWith("!")) commands.add(first);
  }
  return [
    "Read", "Glob", "Grep", "Edit", "Write", "TodoWrite",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    ...[...commands].map((c) => `Bash(${c}:*)`),
    "mcp__autonome__search", "mcp__autonome__conventions_get", "mcp__autonome__plan_get",
  ];
}

function now(): string {
  return new Date().toISOString();
}

function gates(cwd: string, base: string): { passed: boolean; summary: string } {
  const r = spawnSync(join(PLUGIN_ROOT, "bin", "autonome-gates"), ["--json", "--base", base], {
    cwd, encoding: "utf8", env: { ...process.env, AUTONOME_BASE_SHA: base },
  });
  try {
    const out = JSON.parse(r.stdout) as { passed: boolean; outcomes: { gate: string; passed: boolean; output: string }[] };
    const failed = out.outcomes.find((o) => !o.passed);
    return { passed: out.passed, summary: failed ? `${failed.gate} failed: ${failed.output.slice(-1500)}` : "all gates passed" };
  } catch {
    return { passed: false, summary: `autonome-gates did not run: ${r.stderr || r.stdout}`.slice(0, 2000) };
  }
}

function metronomeState(sessionId: string): { status?: string; reason?: string } {
  const p = join(AUTONOME_HOME, "state", `${sessionId}.json`);
  return existsSync(p) ? JSON.parse(readFileSync(p, "utf8")) : {};
}

function transcriptPath(sessionId: string): string | null {
  const root = join(homedir(), ".claude", "projects");
  for (const dir of existsSync(root) ? readdirSync(root) : []) {
    const p = join(root, dir, `${sessionId}.jsonl`);
    if (existsSync(p)) return p;
  }
  return null;
}

/** The scribe: a separate agent writes the handoff of the session, linked to the task. */
function writeHandoff(sessionId: string, cwd: string, taskId: number, log: ConductOptions["log"]): void {
  const transcript = transcriptPath(sessionId);
  if (!transcript) return log(`  no transcript found for ${sessionId}; skipping handoff`);
  const dir = join(AUTONOME_HOME, "logs");
  mkdirSync(dir, { recursive: true });
  const payload = join(dir, `conduct-${sessionId}.json`);
  writeFileSync(payload, JSON.stringify({ session_id: sessionId, transcript_path: transcript, cwd, task_id: taskId }));
  const r = spawnSync("python3", [join(PLUGIN_ROOT, "hooks", "write_handoff.py"), payload], { cwd, encoding: "utf8" });
  log(r.status === 0 ? `  handoff written` : `  handoff failed: ${r.stderr.slice(-500)}`);
}

async function play(task: Task, worktree: string, base: string, opts: ConductOptions) {
  const cfg = (await context.gateConfig(opts.project)).config;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), cfg.budget.task_minutes * 60_000);
  let sessionId = "";
  let tokens = 0;
  let cost = 0;
  let stop = "";
  try {
    for await (const msg of query({
      prompt: `Play task #${task.id}: ${task.body.title}. Read the briefing, then implement it.`,
      options: {
        cwd: worktree,
        model: opts.model,
        abortController: abort,
        permissionMode: "dontAsk",
        allowedTools: allowedTools(cfg),
        settingSources: ["project"],
        plugins: [{ type: "local", path: PLUGIN_ROOT }],
        systemPrompt: { type: "preset", preset: "claude_code", append: MUSICIAN_PROMPT },
        env: {
          ...process.env,
          AUTONOME_CONDUCT: "1",
          AUTONOME_TASK_ID: String(task.id),
          AUTONOME_BASE_SHA: base,
          AUTONOME_PROJECT: opts.project,
          PATH: `${join(PLUGIN_ROOT, "bin")}:${process.env.PATH}`,
        } as Record<string, string>,
      },
    })) {
      if (msg.type === "system" && msg.subtype === "init") sessionId = msg.session_id;
      if (msg.type === "assistant") {
        const u = msg.message.usage;
        tokens += (u?.input_tokens ?? 0) + (u?.output_tokens ?? 0) + (u?.cache_creation_input_tokens ?? 0);
        if (tokens > cfg.budget.task_tokens) {
          stop = `token budget exceeded (${tokens} > ${cfg.budget.task_tokens})`;
          abort.abort();
        }
      }
      if (msg.type === "result") {
        cost = msg.total_cost_usd;
        if (msg.subtype !== "success") stop ||= msg.subtype;
      }
    }
  } catch (e) {
    stop ||= abort.signal.aborted ? `time budget exceeded (${cfg.budget.task_minutes} min)` : String(e);
  } finally {
    clearTimeout(timer);
  }
  return { sessionId, tokens, cost, stop };
}

/** Rebase the task's branch on main, re-check the gates, and fast-forward main. */
function integrate(repo: string, worktree: string, branch: string, push: boolean): { ok: boolean; reason?: string } {
  const rebase = tryGit(worktree, "rebase", "main");
  if (!rebase.ok) {
    tryGit(worktree, "rebase", "--abort");
    return { ok: false, reason: `conflicts with main: ${rebase.out.slice(-800)}` };
  }
  const check = gates(worktree, "main");
  if (!check.passed) return { ok: false, reason: `gates failed on top of main: ${check.summary}` };
  const ff = fastForwardMain(repo, branch);
  if (!ff.ok) return ff;
  if (push) {
    const p = tryGit(repo, "push", "origin", "main");
    if (!p.ok) return { ok: false, reason: `merged locally but push failed: ${p.out}` };
  }
  tryGit(repo, "worktree", "remove", "--force", worktree);
  tryGit(repo, "branch", "-D", branch);
  return { ok: true };
}

async function conductTask(task: Task, opts: ConductOptions): Promise<void> {
  const { repo, log } = opts;
  if (task.status === "released") {
    const last = task.body.runs?.at(-1);
    if (!last) return void (await context.transition(task.id, "pending"));
    log(`#${task.id} released at the fermata; merging ${last.branch}`);
    const r = integrate(repo, last.worktree, last.branch, opts.push);
    await context.transition(task.id, r.ok ? "done" : "pending",
      { ...last, at: now(), outcome: r.ok ? "merged" : "merge-failed", reason: r.reason });
    return log(r.ok ? `  merged` : `  could not merge: ${r.reason}`);
  }

  const base = git(repo, "rev-parse", "main");
  const branch = `autonome/task-${task.id}-${Date.now().toString(36)}`;
  const worktree = join(dirname(repo), ".autonome-worktrees", branch.replaceAll("/", "-"));
  git(repo, "worktree", "add", "-q", "-b", branch, worktree, base);
  log(`#${task.id} ${task.body.title} — playing in ${worktree}`);

  const result = await play(task, worktree, base, opts);
  const run: Run = { at: now(), session_id: result.sessionId, branch, worktree, base, outcome: "",
    tokens: result.tokens, cost_usd: result.cost };
  const block = async (reason: string) => {
    log(`  blocked: ${reason}`);
    await context.transition(task.id, "blocked", { ...run, outcome: "blocked", reason });
    tryGit(repo, "worktree", "remove", "--force", worktree); // the branch stays for inspection
  };

  const state = result.sessionId ? metronomeState(result.sessionId) : {};
  if (result.sessionId) writeHandoff(result.sessionId, worktree, task.id, log);
  if (result.stop) return block(result.stop);
  if (state.status === "blocked") return block(`metronome: ${state.reason}`);

  git(worktree, "add", "-A");
  if (!git(worktree, "status", "--porcelain")) return block("the session changed nothing");
  const check = gates(worktree, base);
  if (!check.passed) return block(check.summary);
  execFileSync("git", ["commit", "-q", "-F", "-"], { cwd: worktree, input: commitMessage(task) });

  if (task.body.fermata) {
    log(`  green; held at the fermata for your review`);
    return void (await context.transition(task.id, "on_hold", {
      ...run, outcome: "on_hold",
      diff_stat: git(worktree, "diff", "--stat", `${base}..HEAD`),
      diff: git(worktree, "diff", `${base}..HEAD`).slice(0, 60_000),
    }));
  }
  const r = integrate(repo, worktree, branch, opts.push);
  if (!r.ok) return block(r.reason ?? "could not merge");
  await context.transition(task.id, "done", { ...run, outcome: "merged" });
  log(`  merged into main`);
}

export async function conduct(opts: ConductOptions): Promise<number> {
  await context.health().catch(() => {
    throw new Error("autonome-context is unreachable; the conductor does not play without it");
  });
  if (git(opts.repo, "rev-parse", "--show-toplevel") !== resolve(opts.repo)) {
    throw new Error(`${opts.repo} is not the root of a git repository`);
  }
  let played = 0;
  while (played < opts.maxTasks) {
    const { task } = await context.claim(opts.project);
    if (!task) break;
    try {
      await conductTask(task, opts);
    } catch (e) {
      opts.log(`#${task.id} failed unexpectedly: ${String(e)}`);
      await context.transition(task.id, task.status === "released" ? "pending" : "blocked", {
        at: now(), branch: "", worktree: "", base: "", outcome: "error", reason: String(e).slice(0, 2000),
      }).catch(() => undefined);
    }
    played++;
  }
  opts.log(played ? `played ${played} task(s)` : "nothing to play");
  return played;
}

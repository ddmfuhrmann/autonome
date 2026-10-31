// The maestro: takes approved tasks one by one, plays each in its own session and worktree, and
// only lets work reach main when the gates agree.
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { consult } from "./agents.js";
import { context, type GateConfig, type Plan, type Rehearsal, type Run, type Task } from "./context.js";
import { findingsForEncore, review } from "./review.js";
import { soundcheck } from "./soundcheck.js";
import { commitMessage, fastForwardMain, git, mainCheckout, tryGit } from "./git.js";

export const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export const AUTONOME_HOME = process.env.AUTONOME_HOME ?? join(homedir(), ".autonome");

export interface ConductOptions {
  project: string;
  repo: string;
  model?: string;
  maxTasks: number;
  push: boolean;
  /** Keep waiting for newly approved work instead of stopping when the queue is empty (seconds between checks). */
  watch?: number;
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
export function allowedTools(cfg: Pick<GateConfig["config"], "gates" | "allowed_commands">): string[] {
  const commands = new Set<string>(["autonome-gates", ...(cfg.allowed_commands ?? [])]);
  for (const g of cfg.gates) {
    const first = g.run.trim().split(/\s+/)[0];
    if (first && !first.startsWith("!")) commands.add(first);
  }
  return [
    "Read", "Glob", "Grep", "Edit", "Write", "TodoWrite",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    ...[...commands].map((c) => `Bash(${c}:*)`),
    // The memory, under the plugin's server name (the musician loads the plugin) and the plain one.
    ...["search", "conventions_get", "plan_get"].flatMap((t) => [`mcp__autonome__${t}`, `mcp__plugin_autonome_autonome__${t}`]),
  ];
}

const REHEARSAL_PROMPT = `You rehearse a plan of the autonome process before it plays. It was written and
approved together with other features of a wave; those are merged now, so the code may differ from what the
plan assumed. Nobody will answer questions.

Read the plan below, then the code it touches and what changed since it was written (git log, and the
project memory via the autonome MCP: handoffs and decisions of the features merged before it).

- verdict "ready": the plan still fits. Put in \`notes\` what the implementers should know about the code as
  it is now (real file, class and function names, things the plan names differently). Small mismatches
  belong here, not in a revision.
- verdict "revise": only when a task would do the wrong thing or something it needs is missing (a task is
  already done, an assumption is false, the order no longer works). Return the whole revised plan in
  \`plan\`, same shape as the original, same goal and out of scope; change only what must change. The
  human approves it before anything plays.`;

const PLAN_SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["ready", "revise"] },
    notes: { type: "string" },
    plan: { type: "object", description: "the full revised plan, only when the verdict is revise" },
  },
  required: ["verdict", "notes"],
} as const;

/** The rehearsal: a read-only session checks a wave plan against main before its first task plays. */
export async function rehearse(plan: Plan, opts: ConductOptions): Promise<Rehearsal> {
  const { title, ...rest } = plan.body;
  delete rest.rehearsal;
  const { answer } = await consult<Rehearsal>({
    cwd: mainCheckout(opts.repo) ?? opts.repo,
    model: opts.model,
    role: REHEARSAL_PROMPT,
    schema: PLAN_SCHEMA,
    prompt: `Rehearse plan #${plan.id} (feature ${plan.feature ?? "-"}, project ${opts.project}):\n\n` +
      JSON.stringify({ title, ...rest }, null, 2),
  });
  return answer;
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

/** One session of the musician; `encore` resumes it with what to fix. */
async function play(task: Task, worktree: string, base: string, opts: ConductOptions,
  encore?: { sessionId: string; prompt: string }) {
  const cfg = (await context.gateConfig(opts.project)).config;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), cfg.budget.task_minutes * 60_000);
  let sessionId = "";
  let tokens = 0;
  let cost = 0;
  let stop = "";
  try {
    for await (const msg of query({
      prompt: encore?.prompt ?? `Play task #${task.id}: ${task.body.title}. Read the briefing, then implement it.`,
      options: {
        resume: encore?.sessionId,
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

/** The scribe keeps the living docs true to main after each delivery. A failure does not undo the
 * merge; the doc-drift cue picks the feature up later. */
export function updateDocs(repo: string, project: string, args: string[], log: ConductOptions["log"]): void {
  const cwd = mainCheckout(repo) ?? repo;
  const r = spawnSync(join(PLUGIN_ROOT, "bin", "autonome-docs"), ["--project", project, ...args], { cwd, encoding: "utf8" });
  log(r.status === 0 ? `  docs: ${r.stdout.trim()}` : `  docs update failed: ${r.stderr.slice(-400)}`);
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
    log(r.ok ? `  merged` : `  could not merge: ${r.reason}`);
    if (r.ok && task.feature) updateDocs(repo, opts.project, ["--feature", task.feature], log);
    return;
  }

  const base = git(repo, "rev-parse", "main");
  const branch = `autonome/task-${task.id}-${Date.now().toString(36)}`;
  const worktree = join(dirname(repo), ".autonome-worktrees", branch.replaceAll("/", "-"));
  git(repo, "worktree", "add", "-q", "-b", branch, worktree, base);
  log(`#${task.id} ${task.body.title} — playing in ${worktree}`);

  const cfg = (await context.gateConfig(opts.project)).config;
  let result = await play(task, worktree, base, opts);
  const run: Run = { at: now(), session_id: result.sessionId, branch, worktree, base, outcome: "",
    tokens: result.tokens, cost_usd: result.cost };
  const block = async (reason: string) => {
    log(`  blocked: ${reason}`);
    await context.transition(task.id, "blocked", { ...run, outcome: "blocked", reason });
    tryGit(repo, "worktree", "remove", "--force", worktree); // the branch stays for inspection
  };

  // Gates, then the reviewer; blocking findings send the musician back to the same session.
  const judge = async (): Promise<string | null> => {
    for (let round = 0; ; round++) {
      const state = result.sessionId ? metronomeState(result.sessionId) : {};
      if (result.stop) return result.stop;
      if (state.status === "blocked") return `metronome: ${state.reason}`;
      git(worktree, "add", "-A");
      if (!git(worktree, "status", "--porcelain")) return "the session changed nothing";
      const check = gates(worktree, base);
      if (!check.passed) return check.summary;
      if (!cfg.review.enabled) return null;
      const briefing = (await context.briefing(opts.project, task.id)).text;
      const rv = await review(task, briefing, worktree, base, opts.model);
      run.review = rv.result;
      run.review_rounds = round + 1;
      run.cost_usd = (run.cost_usd ?? 0) + rv.cost;
      if (rv.result.verdict === "pass") return null;
      const high = findingsForEncore(rv.result);
      if (round >= cfg.review.max_rounds) return `review: still blocking after ${round} encore(s):\n${high}`;
      log(`  review: blocking findings; encore ${round + 1}`);
      result = await play(task, worktree, base, opts, {
        sessionId: result.sessionId,
        prompt: `An independent reviewer read your diff and found blocking issues. Fix them, then end your turn:\n${high}`,
      });
      run.tokens = (run.tokens ?? 0) + result.tokens;
      run.cost_usd = (run.cost_usd ?? 0) + result.cost;
    }
  };
  const failure = await judge();
  if (result.sessionId) writeHandoff(result.sessionId, worktree, task.id, log);
  if (failure) return block(failure);
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
  if (task.feature) updateDocs(repo, opts.project, ["--feature", task.feature], log);
}

async function soundcheckPlan(plan: Plan, opts: ConductOptions): Promise<void> {
  const cfg = (await context.gateConfig(opts.project)).config.soundcheck;
  if (!cfg) return;
  opts.log(`plan #${plan.id} ${plan.body.title}: every task merged; soundcheck`);
  const r = await soundcheck(plan, cfg, mainCheckout(opts.repo) ?? opts.repo, opts.model, opts.log);
  const { outcome } = await context.soundcheck(plan.id, r);
  opts.log(outcome === "done" ? `  passed; the feature is done`
    : outcome === "fixing" ? `  failed; ${r.fixes.length} fix task(s) queued`
    : `  failed; the plan is blocked for your decision`);
}

export async function conduct(opts: ConductOptions): Promise<number> {
  await context.health().catch(() => {
    throw new Error("autonome-context is unreachable; the conductor does not play without it");
  });
  if (git(opts.repo, "rev-parse", "--show-toplevel") !== resolve(opts.repo)) {
    throw new Error(`${opts.repo} is not the root of a git repository`);
  }
  let played = 0;
  let sinceOverview = 0;
  while (played < opts.maxTasks) {
    const { task, rehearse: plan, soundcheck: finished } = await context.claim(opts.project);
    if (finished) {
      await soundcheckPlan(finished, opts);
      continue;
    }
    if (!task) {
      if (!opts.watch) break;
      if (sinceOverview) updateDocs(opts.repo, opts.project, ["--overview"], opts.log);
      sinceOverview = 0;
      await new Promise((r) => setTimeout(r, opts.watch! * 1000));
      continue;
    }
    try {
      if (plan) {
        opts.log(`plan #${plan.id} ${plan.body.title}: rehearsing against main`);
        const r = await rehearse(plan, opts);
        const { plan_status } = await context.rehearsal(plan.id, r);
        if (plan_status === "held") {
          opts.log(`  the plan needs revising; held for your approval`);
          await context.transition(task.id, "pending");
          continue;
        }
        opts.log(`  ready`);
      }
      await conductTask(task, opts);
    } catch (e) {
      opts.log(`#${task.id} failed unexpectedly: ${String(e)}`);
      await context.transition(task.id, task.status === "released" ? "pending" : "blocked", {
        at: now(), branch: "", worktree: "", base: "", outcome: "error", reason: String(e).slice(0, 2000),
      }).catch(() => undefined);
    }
    played++;
    sinceOverview++;
  }
  if (sinceOverview) updateDocs(opts.repo, opts.project, ["--overview"], opts.log);
  opts.log(played ? `played ${played} task(s)` : "nothing to play");
  return played;
}

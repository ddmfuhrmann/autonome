// The runner: takes approved tasks one by one, runs each in its own session and worktree, and
// only lets work reach main when the gates agree.
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { consult } from "./agents.js";
import { ContextError, context, languageRule, roleModel, sdkModel, waitForContext, type Effort, type GateConfig, type Plan, type Preflight, type ReviewResult, type Run, type Task } from "./context.js";
import { doctor, report } from "./doctor.js";
import { bashPrefixes, scopedFileCommands } from "./fsguard.js";
import { retryMessage, findingsForRetry, gatesSection, recordRound, review, settleRun, type GateOutcome } from "./review.js";
import { smokeTest } from "./smoke-test.js";
import { jevKey, sessionEnv } from "./secrets.js";
import { triagePending, triageRevisions } from "./triage-run.js";
import type { askJev } from "./triage.js";
import { advisoryLine } from "./triage.js";
import { isUsageLimit, UsageLimitError, untilReset } from "./limits.js";
import { retroFinishedWaves } from "./retro.js";
import { EventStream, fromMessage } from "./events.js";

/** Forwards a session's SDK messages to the live view under a role. */
const live = (opts: RunOptions, role: string, owner: { task_id?: number; plan_id?: number }) =>
  (msg: unknown) => fromMessage(msg).forEach((e) => opts.events?.push({ ...e, role, ...owner }));
const stage = (opts: RunOptions, owner: { task_id?: number; plan_id?: number }, text: string) =>
  opts.events?.push({ role: "runner", kind: "stage", text, ...owner });
import { applyWip, commitMessage, commitWip, fastForwardMain, git, mainCheckout, stageWork, strayFiles, tryGit, wipMessage } from "./git.js";

export const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export const AUTONOME_HOME = process.env.AUTONOME_HOME ?? join(homedir(), ".autonome");

export interface RunOptions {
  project: string;
  repo: string;
  model?: string;
  /** Overrides every role's effort (the gate config's `models` sets it per role). */
  effort?: Effort;
  maxTasks: number;
  push: boolean;
  /** Keep waiting for newly approved work instead of stopping when the queue is empty (seconds between checks). */
  watch?: number;
  log: (line: string) => void;
  /** Where the live view's events go; the run makes one when none is given. */
  events?: EventStream;
  /** Tests replace the agent sessions and Jev's API with fakes. */
  seams?: { query?: typeof query; preflight?: typeof preflight; ask?: typeof askJev; runTask?: (task: Task, opts: RunOptions) => Promise<Run | undefined>;
    gates?: JudgeSeams["gates"]; review?: typeof review; commitWip?: typeof commitWip };
}

/** How often a running session looks at the project's pause (at most), in ms. */
const PAUSE_CHECK_MS = 15_000;

export const RESUME_PROMPT = "This session was paused and is resuming. A command that was running when it stopped was killed: " +
  "check the state of the worktree and run it again before going on.";

const IMPLEMENTER_PROMPT = `You are the implementer of one task in the autonome process. The briefing injected at
the start of this session has the task, its plan, the conventions for its layers and the gates.

- Do exactly the task. Nothing listed as out of scope, no drive-by refactors.
- Prove each acceptance criterion with a test or a gate where possible.
- While working, run the fast gates with \`autonome-gates --fast\`.
- Do not commit, push, switch branches or touch git history: the runner does that. It commits every
  file you leave in the tree that .gitignore does not exclude; to keep a generated file (coverage data,
  build output, a local database) out, add it to .gitignore.
- Never edit, weaken or skip a gate or the rules behind it to make it pass.
- {{SHELL}}
- Run tools exactly the way the gates do (e.g. \`uv run pytest\`, \`uv run ruff format .\`), never a bare
  \`python\`, \`pytest\` or \`ruff\`. One plain command per Bash call: no subshells, \`;\` chains or
  \`cd\`. Read and search with the Read, Grep and Glob tools, not \`cat\`, \`grep\` or \`find\`. The gate
  config is \`gate_config_get\` in the autonome memory.
- A command that runs for more than 2 minutes without finishing is investigated, never waited for: run it
  again with a timeout (e.g. \`-x\` and a per-test timeout) to find what hangs, and fix that.
- Nobody will answer questions. If something is truly ambiguous, choose the option most consistent
  with the plan and the conventions, and say so in your final message.
- When done, end your turn with a short summary. The gate hook then runs every gate and may send you
  back with a failure (a retry): fix the cause, not the symptom.`;

const SHELL_RULE: Record<"allowlist" | "auto", string> = {
  allowlist: `Create and change files only with the Write and Edit tools. The shell is for running things: the
  gates' commands, \`autonome-gates --fast\`, read-only git and inspection. Writing through the shell
  (heredocs, \`cat >\`, \`python -c\`, \`sed -i\`) is denied in this session, and so is \`git -C\`:
  you are already in the right directory.`,
  auto: `Create and change files with the Write and Edit tools. For mechanical changes across many files
  (a rename, swapping a pattern) use a script or \`sed -i\` instead of one Edit per occurrence. Git that
  changes history (commit, push, reset, checkout, switch, rebase, merge, tag, branch, stash, \`git -C\`)
  stays forbidden: you are already in the right directory.`,
};

/** The implementer's system prompt for the project's permissions mode. */
export function implementerPrompt(gc: GateConfig): string {
  return IMPLEMENTER_PROMPT.replace("{{SHELL}}", SHELL_RULE[gc.config.permissions === "auto" ? "auto" : "allowlist"]);
}

/**
 * What an implementer may do without asking (nobody is there to ask): read and edit files in its
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
    // git refuses paths outside the repository, so these stay inside the worktree on their own.
    "Bash(git rm:*)", "Bash(git mv:*)", "Bash(git update-index:*)", "Bash(git add:*)", "Bash(git check-ignore:*)",
    "Bash(git ls-files:*)", "Bash(git rev-parse:*)", "Bash(git merge-base:*)",
    // Read-only looks around: what is installed, what a jar holds (find goes through the hook).
    "Bash(ls:*)", "Bash(which:*)", "Bash(unzip -l:*)", "Bash(jar tf:*)",
    // Read-only helpers sessions reach for; writing goes through Edit/Write, never the shell.
    "Bash(sed -n:*)", "Bash(echo:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    ...[...commands].map((c) => `Bash(${c}:*)`),
    // The memory, under the plugin's server name (the implementer loads the plugin) and the plain one.
    ...["search", "conventions_get", "plan_get", "gate_config_get", "history_get"].flatMap((t) => [`mcp__autonome__${t}`, `mcp__plugin_autonome_autonome__${t}`]),
  ];
}

export const PREFLIGHT_PROMPT = `You run the preflight of a plan of the autonome process before it runs. It was written and
approved together with other features of a wave; those are merged now, so the code may differ from what the
plan assumed. Nobody will answer questions.

Read the plan below, then the code it touches and what changed since it was written (git log, and the
project memory via the autonome MCP: handoffs and decisions of the features merged before it, and
\`history_get\` for what went off plan on this subject; a lesson the plan ignores is a reason to revise).

Check it against the gates too. For each gate below, read the rules it enforces in the repo (import
contracts, type checker strictness, lint rules, coverage floors: pyproject.toml, setup.cfg, .importlinter,
tsconfig.json, eslint and similar). A task whose spec cannot pass a gate as written (e.g. a module the
spec makes import something a contract forbids) is a conflict between plans: verdict "revise", and the
revision says which rule and which spec statements clash and changes the spec, or adds a task that
changes the rule when the rule is what is wrong. Never weaken a gate quietly.

- verdict "ready": the plan still fits. Put in \`notes\` what the implementers should know about the code as
  it is now (real file, class and function names, things the plan names differently). Small mismatches
  belong here, not in a revision.
- verdict "revise": only when a task would do the wrong thing or something it needs is missing (a task is
  already done, an assumption is false, the order no longer works). Return the whole revised plan in
  \`plan\`, same shape as the original, same goal and out of scope; change only what must change. The
  human approves it before anything runs.`;

export const PLAN_SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["ready", "revise"] },
    notes: { type: "string" },
    plan: { type: "object", description: "the full revised plan, only when the verdict is revise" },
  },
  required: ["verdict", "notes"],
} as const;

/** The preflight: a read-only session checks a wave plan against main before its first task runs. */
export function preflightPrompt(plan: Plan, project: string, gates: GateConfig["config"]["gates"]): string {
  const { title, ...rest } = plan.body;
  delete rest.preflight;
  return `Preflight of plan #${plan.id} (feature ${plan.feature ?? "-"}, project ${project}):\n\n` +
    JSON.stringify({ title, ...rest }, null, 2) +
    `\n\n## Gates it must pass\n${gates.map((g) => `- ${g.name}: \`${g.run}\``).join("\n") || "- (none configured)"}`;
}

export async function preflight(plan: Plan, opts: RunOptions): Promise<Preflight> {
  const { gates } = (await context.gateConfig(opts.project)).config;
  const cfg = (await context.gateConfig(opts.project)).config;
  const { answer } = await consult<Preflight>({
    cwd: mainCheckout(opts.repo) ?? opts.repo,
    ...roleModel(cfg, "preflight", opts),
    role: PREFLIGHT_PROMPT,
    language: languageRule(cfg),
    schema: PLAN_SCHEMA,
    prompt: preflightPrompt(plan, opts.project, gates),
    onMessage: live(opts, "preflight", { plan_id: plan.id }),
  });
  return answer;
}

/** The implementer may also run the commands of gates still pending from approved plans: on a greenfield
 * project the setup task that installs them (e.g. `uv add --dev mypy`) is the first thing to run. */
export function implementerTools(gc: GateConfig): string[] {
  return allowedTools({ ...gc.config, gates: [...gc.config.gates, ...(gc.pending ?? [])] });
}

/** Hard limits that hold in every permissions mode, whatever the classifier thinks. */
const HARD_DENIALS = [
  ...["commit", "push", "reset", "checkout", "switch", "rebase", "merge", "tag", "branch", "stash", "-C"].map((c) => `Bash(git ${c}:*)`),
];
// A session never waits on a command: Monitor is off, and the fsguard hook denies background Bash.
const NO_WAITING = ["Monitor"];
const SECRET_DENIALS = ["Read(~/.autonome/**)", "Edit(~/.autonome/**)", "Bash(cat ~/.autonome:*)"];

export interface ImplementerPermissions {
  permissionMode: "dontAsk" | "auto";
  permissionPrompts?: "none";
  allowedTools: string[];
  disallowedTools: string[];
  hooks: { PreToolUse: ReturnType<typeof scopedFileCommands>[] };
}

/** What the implementer's session may do: `allowlist` (the default) or `auto` (the SDK's classifier
 * judges what the allowlist does not cover). Git that changes history, `~/.autonome` and files outside
 * the worktree stay denied by rule in both. */
export function implementerPermissions(gc: GateConfig, worktree: string): ImplementerPermissions {
  const allowedTools = implementerTools(gc);
  const hooks = { PreToolUse: [scopedFileCommands(worktree, bashPrefixes(allowedTools))] };
  if (gc.config.permissions === "auto") {
    return { permissionMode: "auto", permissionPrompts: "none", allowedTools,
      disallowedTools: [...HARD_DENIALS, ...SECRET_DENIALS, ...NO_WAITING], hooks };
  }
  return { permissionMode: "dontAsk", allowedTools, disallowedTools: [...HARD_DENIALS, ...SECRET_DENIALS, ...NO_WAITING], hooks };
}

function now(): string {
  return new Date().toISOString();
}

function gates(cwd: string, base: string): { passed: boolean; summary: string; outcomes?: GateOutcome[] } {
  const r = spawnSync(join(PLUGIN_ROOT, "bin", "autonome-gates"), ["--json", "--base", base], {
    cwd, encoding: "utf8", env: sessionEnv({ AUTONOME_BASE_SHA: base }),
  });
  try {
    const out = JSON.parse(r.stdout) as { passed: boolean; outcomes: GateOutcome[] };
    const failed = out.outcomes.find((o) => !o.passed);
    return { passed: out.passed, outcomes: out.outcomes,
      summary: failed ? `${failed.gate} failed: ${failed.output.slice(-1500)}` : "all gates passed" };
  } catch {
    return { passed: false, summary: `autonome-gates did not run: ${r.stderr || r.stdout}`.slice(0, 2000) };
  }
}

function gateHookState(sessionId: string): { status?: string; reason?: string } {
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

/** The handoff writer: a separate agent writes the handoff of the session, linked to the task. */
function writeHandoff(sessionId: string, cwd: string, taskId: number, project: string, log: RunOptions["log"]): void {
  const transcript = transcriptPath(sessionId);
  if (!transcript) return log(`  no transcript found for ${sessionId}; skipping handoff`);
  const dir = join(AUTONOME_HOME, "logs");
  mkdirSync(dir, { recursive: true });
  const payload = join(dir, `run-${sessionId}.json`);
  writeFileSync(payload, JSON.stringify({ session_id: sessionId, transcript_path: transcript, cwd, task_id: taskId }));
  const r = spawnSync("python3", [join(PLUGIN_ROOT, "hooks", "write_handoff.py"), payload], {
    cwd, encoding: "utf8", env: sessionEnv({ AUTONOME_PROJECT: project }) });
  log(r.status === 0 ? `  handoff written` : `  handoff failed: ${r.stderr.slice(-500)}`);
}

/** One session of the implementer; `retry` resumes it with what to fix, `note` is added to a first prompt. */
export async function implement(task: Task, worktree: string, base: string, opts: RunOptions,
  retry?: { sessionId: string; prompt: string }, note?: string) {
  const gateConfig = await context.gateConfig(opts.project);
  const cfg = gateConfig.config;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), cfg.budget.task_minutes * 60_000);
  let sessionId = "";
  let tokens = 0;
  let cost = 0;
  let stop = "";
  let lastPauseCheck = 0;
  try {
    const q = (opts.seams?.query ?? query)({
      prompt: retry?.prompt ?? `Run task #${task.id}: ${task.body.title}. Read the briefing, then implement it.${note ? `\n\n${note}` : ""}`,
      options: {
        resume: retry?.sessionId,
        cwd: worktree,
        ...sdkModel(roleModel(cfg, "implementer", opts)),
        abortController: abort,
        ...implementerPermissions(gateConfig, worktree),
        settingSources: ["project"],
        plugins: [{ type: "local", path: PLUGIN_ROOT }],
        systemPrompt: { type: "preset", preset: "claude_code", append: implementerPrompt(gateConfig) },
        env: sessionEnv({
          AUTONOME_RUN: "1",
          AUTONOME_TASK_ID: String(task.id),
          AUTONOME_BASE_SHA: base,
          AUTONOME_PROJECT: opts.project,
          BASH_DEFAULT_TIMEOUT_MS: "120000",
          BASH_MAX_TIMEOUT_MS: "300000",
          PATH: `${join(PLUGIN_ROOT, "bin")}:${process.env.PATH}`,
        }),
      },
    });
    for await (const msg of q) {
      if (msg.type === "system" && msg.subtype === "init") sessionId = msg.session_id;
      live(opts, "implementer", { task_id: task.id })(msg);
      // A paused queue stops the session where it is; the worktree and the session are kept.
      if (!stop && Date.now() - lastPauseCheck >= PAUSE_CHECK_MS) {
        lastPauseCheck = Date.now();
        if (await context.projectPause(opts.project)) {
          stop = "paused";
          await q.interrupt();
        }
      }
      if (msg.type === "assistant") {
        const u = msg.message.usage;
        tokens += (u?.input_tokens ?? 0) + (u?.output_tokens ?? 0) + (u?.cache_creation_input_tokens ?? 0);
        if (!stop && tokens > cfg.budget.task_tokens) {
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
    if (e instanceof ContextError) throw e; // not the session's fault, and never a pause
    stop ||= abort.signal.aborted ? `time budget exceeded (${cfg.budget.task_minutes} min)` : String(e);
  } finally {
    clearTimeout(timer);
  }
  return { sessionId, tokens, cost, stop };
}

/** The docs agent keeps the living docs true to main after each delivery. A failure does not undo the
 * merge; the doc-drift schedule picks the feature up later. */
export function updateDocs(repo: string, project: string, args: string[], log: RunOptions["log"]): void {
  const cwd = mainCheckout(repo) ?? repo;
  const r = spawnSync(join(PLUGIN_ROOT, "bin", "autonome-docs"), ["--project", project, ...args], { cwd, encoding: "utf8", env: sessionEnv() });
  log(r.status === 0 ? `  docs: ${r.stdout.trim()}` : `  docs update failed: ${r.stderr.slice(-400)}`);
}

/** Rebase the task's branch on main, re-check the gates, and fast-forward main. */
async function integrate(repo: string, worktree: string, branch: string, push: boolean,
  hooks: { gates?: JudgeSeams["gates"]; paused?: () => Promise<boolean> } = {}): Promise<{ ok: boolean; reason?: string; paused?: boolean }> {
  const rebase = tryGit(worktree, "rebase", "main");
  if (!rebase.ok) {
    tryGit(worktree, "rebase", "--abort");
    return { ok: false, reason: `conflicts with main: ${rebase.out.slice(-800)}` };
  }
  const check = (hooks.gates ?? gates)(worktree, "main");
  if (!check.passed) return { ok: false, reason: `gates failed on top of main: ${check.summary}` };
  // The gates took a while: last look before main moves.
  if (await hooks.paused?.()) return { ok: false, paused: true };
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

/** Thrown when a task reached main but the context could not record it; the next run does. */
class MergedUnrecorded extends ContextError {}

function journalPath(project: string, taskId: number): string {
  return join(AUTONOME_HOME, "state", `merged-${project}-${taskId}.json`);
}

/** Main moved: the context must learn it, now or on the next run, so the task never runs twice. */
async function recordMerged(task: Task, run: Run, opts: RunOptions): Promise<void> {
  try {
    await context.transition(task.id, "done", run);
  } catch (e) {
    if (!(e instanceof ContextError)) throw e;
    mkdirSync(join(AUTONOME_HOME, "state"), { recursive: true });
    writeFileSync(journalPath(opts.project, task.id), JSON.stringify({ task_id: task.id, run }));
    throw new MergedUnrecorded(`${e.message}\n  #${task.id} is merged into main; it will be recorded as done on the next run`);
  }
}

/** Records merges an earlier run could not report. */
async function replayJournal(opts: RunOptions): Promise<void> {
  const dir = join(AUTONOME_HOME, "state");
  const prefix = `merged-${opts.project}-`;
  for (const f of existsSync(dir) ? readdirSync(dir).filter((n) => n.startsWith(prefix)) : []) {
    const { task_id, run } = JSON.parse(readFileSync(join(dir, f), "utf8")) as { task_id: number; run: Run };
    await context.transition(task_id, "done", run).catch((e) => {
      if (e instanceof ContextError) throw e; // still down: keep the journal entry
      opts.log(`#${task_id}: ${String(e)} (already recorded?)`);
    });
    rmSync(join(dir, f));
    opts.log(`#${task_id} merged earlier; recorded as done now`);
  }
}

/** The reviewer's verdict for the live view: the verdict, then one line per finding. */
export function verdictLine(r: ReviewResult): string {
  if (!r.findings.length) return `**${r.verdict}**, no findings`;
  const n = r.findings.length;
  return `**${r.verdict}**, ${n} finding${n > 1 ? "s" : ""}:\n` + r.findings.map((f) =>
    `- ${f.severity} ${f.kind}${f.file ? ` in \`${f.file}\`` : ""}: ${f.issue.replace(/\s+/g, " ")}`).join("\n");
}

/** What `judgeTask` calls out to; tests swap them (a reviewer that fails round 1 and passes round 2). */
export interface JudgeSeams {
  gates: (cwd: string, base: string) => { passed: boolean; summary: string; outcomes?: GateOutcome[] };
  review: typeof review;
  briefing: () => Promise<string>;
}

export interface JudgeInput {
  task: Task;
  cfg: GateConfig["config"];
  opts: RunOptions;
  run: Run;
  worktree: string;
  base: string;
  /** The implementer's latest session; changes after each retry. */
  current: () => { sessionId: string; stop: string };
  retry: (prompt: string) => Promise<void>;
  /** Whether the queue is paused; asked before the gates and before each reviewer round. */
  paused?: () => Promise<boolean>;
}

/** Gates, then the reviewer; blocking findings send the implementer back to the same session.
 * Returns why the task cannot merge (null when it can) and whether the reviewer's findings are the reason. */
export async function judgeTask(input: JudgeInput, seams: JudgeSeams): Promise<{ failure: string | null; blockedOnReview: boolean; paused?: boolean }> {
  const { task, cfg, opts, run, worktree, base, retry } = input;
  const log = opts.log;
  let cleanups = 0;
  let mediumSent = false;
  for (let round = 0; ; ) {
    const result = input.current();
    if (result.stop === "paused") return { failure: null, blockedOnReview: false, paused: true };
    const state = result.sessionId ? gateHookState(result.sessionId) : {};
    if (result.stop && isUsageLimit(result.stop)) throw new UsageLimitError(result.stop);
    if (result.stop) return { failure: result.stop, blockedOnReview: false };
    if (state.status === "blocked") return { failure: `gate hook: ${state.reason}`, blockedOnReview: false };
    stageWork(worktree);
    if (!git(worktree, "status", "--porcelain")) return { failure: "the session changed nothing", blockedOnReview: false };
    // Built in, whatever the project's gates: nothing ignored or unasked-for binary gets committed.
    const stray = strayFiles(worktree, base, `${task.body.title}\n${task.body.spec}`);
    if (stray.length) {
      const list = stray.map((p) => `- ${p}`).join("\n");
      if (cleanups >= cfg.retries.max) return { failure: `stray files still in the change:\n${list}`, blockedOnReview: false };
      cleanups++;
      log(`  stray files in the change; retry ${cleanups}`);
      stage(opts, { task_id: task.id }, `retry: stray files in the change`);
      await retry(`These files would be committed but do not belong in the change. Add them (or their ` +
        `pattern) to .gitignore; the runner restages from scratch, so ignoring them is enough:\n${list}`);
      continue;
    }
    if (await input.paused?.()) return { failure: null, blockedOnReview: false, paused: true };
    stage(opts, { task_id: task.id }, "gates");
    const check = seams.gates(worktree, base);
    stage(opts, { task_id: task.id }, check.passed ? "gates passed" : `gates: ${check.summary.split(":")[0]}`);
    if (!check.passed) return { failure: check.summary, blockedOnReview: false };
    if (!cfg.review.enabled) return { failure: null, blockedOnReview: false };
    if (await input.paused?.()) return { failure: null, blockedOnReview: false, paused: true };
    const briefing = [await seams.briefing(), gatesSection(check.outcomes)].filter(Boolean).join("\n\n");
    stage(opts, { task_id: task.id }, `review, round ${round + 1}`);
    const key = cfg.triage?.model === "jev-latest" ? jevKey((l) => opts.log(`triage: ${l}`)) : undefined;
    const rv = await seams.review(task, briefing, worktree, base,
      { ...roleModel(cfg, "reviewer", opts), language: languageRule(cfg) },
      live(opts, "reviewer", { task_id: task.id }), run.review?.findings, { cfg: cfg.triage, key });
    recordRound(run, rv.result, round + 1);
    opts.events?.push({ task_id: task.id, role: "reviewer", kind: "say", text: verdictLine(rv.result) });
    const checked = advisoryLine(rv.result.findings, cfg.triage?.model === "jev-latest" && !key ? "no Jev key" : undefined);
    if (checked) {
      log(`  ${checked}`);
      opts.events?.push({ task_id: task.id, role: "triage", kind: "say", text: checked });
    }
    run.cost_usd = (run.cost_usd ?? 0) + rv.cost;
    // Medium findings go back once; after that only high ones do, so rounds converge.
    const message = retryMessage(rv.result, round < cfg.review.max_rounds && !mediumSent);
    mediumSent ||= rv.result.verdict === "pass" && !!message;
    if (rv.result.verdict === "pass" && !message) return { failure: null, blockedOnReview: false }; // what is left becomes loose ends at merge
    if (round >= cfg.review.max_rounds) {
      return { failure: `review: still blocking after ${round} retr${round === 1 ? "y" : "ies"}:\n${findingsForRetry(rv.result)}`, blockedOnReview: true };
    }
    round++;
    const why = rv.result.verdict === "pass" ? "medium findings" : "blocking findings";
    log(`  review: ${why}; retry ${round}`);
    stage(opts, { task_id: task.id }, `retry ${round}: back to the implementer with the reviewer's ${why}`);
    await retry(message!);
  }
}

/** Runs one task; returns its last run, for the summary. */
export async function runTask(task: Task, opts: RunOptions): Promise<Run | undefined> {
  const { repo, log } = opts;
  if (task.status === "released") {
    const last = task.body.runs?.at(-1);
    if (!last) return void (await context.transition(task.id, "pending"));
    log(`#${task.id} released from the hold before merge; merging ${last.branch}`);
    const r = await integrate(repo, last.worktree, last.branch, opts.push, { gates: opts.seams?.gates });
    const final: Run = { ...last, at: now(), outcome: r.ok ? "merged" : "merge-failed", reason: r.reason };
    if (r.ok) await recordMerged(task, final, opts);
    else await context.transition(task.id, "pending", final);
    log(r.ok ? `  merged` : `  could not merge: ${r.reason}`);
    if (r.ok && task.feature) updateDocs(repo, opts.project, ["--feature", task.feature], log);
    return final;
  }

  let base = git(repo, "rev-parse", "main");
  let branch = `autonome/task-${task.id}-${Date.now().toString(36)}`;
  let worktree = join(dirname(repo), ".autonome-worktrees", branch.replaceAll("/", "-"));
  let resume: { sessionId: string; prompt: string } | undefined;
  const paused = task.body.runs?.at(-1);
  if (paused?.outcome === "paused") {
    // A paused task goes on where it stopped: same worktree and, when it has one, the same session.
    if (existsSync(paused.worktree)) {
      ({ base, branch, worktree } = paused);
      if (paused.session_id) resume = { sessionId: paused.session_id, prompt: RESUME_PROMPT };
      log(`#${task.id} was paused; resuming in ${worktree}`);
    } else {
      tryGit(repo, "worktree", "prune");
      if (tryGit(repo, "rev-parse", "--verify", `refs/heads/${paused.branch}`).ok) {
        ({ base, branch } = paused);
        worktree = join(dirname(repo), ".autonome-worktrees", `${branch.replaceAll("/", "-")}-${Date.now().toString(36)}`);
        git(repo, "worktree", "add", "-q", worktree, branch);
        log(`#${task.id} was paused but its worktree ${paused.worktree} is gone; new session in a new worktree from ${branch}`);
      } else {
        log(`#${task.id} was paused but its worktree and branch are gone; starting over`);
      }
    }
  }
  const fresh = !existsSync(worktree);
  if (fresh) git(repo, "worktree", "add", "-q", "-b", branch, worktree, base);
  // Try again: the work an earlier attempt kept as WIP is applied on top of main (the session starts new).
  let wipNote: string | undefined;
  if (fresh && !resume && paused?.wip_sha && paused.outcome !== "paused"
    && tryGit(repo, "rev-parse", "--verify", `refs/heads/${paused.branch}`).ok) {
    const why = (paused.reason ?? paused.outcome).split("\n")[0];
    const applied = applyWip(worktree, paused.base, paused.wip_sha);
    wipNote = applied.ok
      ? `A previous attempt stopped: ${why}. Its work (${applied.stat}) is applied in this worktree; check it before going on.`
      : `A previous attempt stopped: ${why}. Its work is on branch ${paused.branch} and did not apply on main; start from main.`;
    log(`#${task.id} starts from the WIP of an earlier attempt (${paused.branch}): ${applied.ok ? "applied" : "did not apply; starting clean"}`);
  }
  log(`#${task.id} ${task.body.title} — running in ${worktree}`);
  stage(opts, { task_id: task.id }, `running on ${branch}`);
  const started = Date.now();

  const cfg = (await context.gateConfig(opts.project)).config;
  const run: Run = { at: now(), session_id: resume?.sessionId, branch, worktree, base, outcome: "", tokens: 0, cost_usd: 0 };
  let result: Awaited<ReturnType<typeof implement>> = { sessionId: "", tokens: 0, cost: 0, stop: "" };
  // Commits what the worktree holds before it goes. Without a commit the worktree stays.
  const keepWork = (reason: string): { ok: boolean; sha: string | null } => {
    try {
      const sha = (opts.seams?.commitWip ?? commitWip)(worktree, wipMessage(task.id, reason))
        // Committed already (the merge failed after the task's commit): that commit is the work.
        ?? (git(worktree, "rev-parse", "HEAD") !== base ? git(worktree, "rev-parse", "HEAD") : null);
      // The diff for Try again starts where the branch leaves main (it moves when the merge rebased the branch).
      const forkPoint = sha ? tryGit(worktree, "merge-base", "main", "HEAD") : undefined;
      if (forkPoint?.ok && forkPoint.out) run.base = forkPoint.out;
      return { ok: true, sha };
    } catch (e) {
      log(`  could not commit the WIP; keeping ${worktree}: ${String(e).split("\n")[0]}`);
      return { ok: false, sha: null };
    }
  };
  // Out of usage: the task runs again later, from its WIP when it has one.
  const onUsageLimit = (e: unknown): UsageLimitError => {
    const err = e instanceof UsageLimitError ? e : new UsageLimitError(String(e));
    const wip = keepWork(err.message);
    if (!wip.ok) return err;
    if (wip.sha) {
      Object.assign(run, { outcome: "usage_limit", reason: err.message, wip_sha: wip.sha,
        session_id: result.sessionId || run.session_id, duration_s: Math.round((Date.now() - started) / 1000) });
      err.run = run;
    }
    tryGit(repo, "worktree", "remove", "--force", worktree);
    if (!wip.sha) tryGit(repo, "branch", "-D", branch);
    return err;
  };
  try {
    result = await implement(task, worktree, base, opts, resume, wipNote);
  } catch (e) {
    if (e instanceof UsageLimitError || isUsageLimit(String(e))) throw onUsageLimit(e);
    throw e;
  }
  Object.assign(run, { session_id: result.sessionId || resume?.sessionId, tokens: result.tokens, cost_usd: result.cost });
  let blockedOnReview = false; // the reviewer's findings are why the task blocks, not a gate or the merge
  const block = async (reason: string) => {
    log(`  blocked: ${reason}`);
    settleRun(run, blockedOnReview ? "blocked" : undefined);
    stage(opts, { task_id: task.id }, `blocked: ${reason}`);
    Object.assign(run, { outcome: "blocked", reason, duration_s: Math.round((Date.now() - started) / 1000) });
    const wip = keepWork(reason);
    if (wip.sha) run.wip_sha = wip.sha;
    await context.transition(task.id, "blocked", run);
    if (wip.ok) tryGit(repo, "worktree", "remove", "--force", worktree); // the branch stays, with the WIP
    return run;
  };

  // Gates, then the reviewer; blocking findings send the implementer back to the same session.
  const retry = async (prompt: string) => {
    result = await implement(task, worktree, base, opts, { sessionId: result.sessionId || run.session_id || "", prompt });
    run.tokens = (run.tokens ?? 0) + result.tokens;
    run.cost_usd = (run.cost_usd ?? 0) + result.cost;
  };
  // The queue was paused: the session and the worktree are kept as they are, and nothing merges.
  const pauseTask = async () => {
    // Only for the log: a context error here must not keep the task from being paused.
    const by = await context.projectPause(opts.project).catch((e) => {
      if (e instanceof ContextError) return null;
      throw e;
    });
    log(`  queue paused${by ? ` by ${by.by}` : ""}; #${task.id} stopped, worktree and session kept`);
    stage(opts, { task_id: task.id }, "paused");
    Object.assign(run, { outcome: "paused", session_id: result.sessionId || run.session_id,
      duration_s: Math.round((Date.now() - started) / 1000) });
    await context.transition(task.id, "paused", run);
    return run;
  };
  const isPaused = async () => !!(await context.projectPause(opts.project));
  let failure: string | null;
  try {
    const judged = await judgeTask({ task, cfg, opts, run, worktree, base, current: () => result, retry, paused: isPaused }, {
      gates: opts.seams?.gates ?? gates,
      review: opts.seams?.review ?? review,
      briefing: async () => (await context.briefing(opts.project, task.id)).text,
    });
    if (judged.paused) return pauseTask();
    failure = judged.failure;
    blockedOnReview = judged.blockedOnReview;
  } catch (e) {
    // Out of usage (in the implementer or a reviewer): the work so far is kept as WIP, the task runs again later.
    if (e instanceof UsageLimitError || isUsageLimit(String(e))) throw onUsageLimit(e);
    throw e;
  }
  if (!failure && (await isPaused())) return pauseTask(); // before the handoff: a paused session is not finished
  if (result.sessionId) {
    writeHandoff(result.sessionId, worktree, task.id, opts.project, log);
    await triagePending(opts.project, cfg, opts);
  }
  if (failure) return block(failure);
  if (await isPaused()) return pauseTask(); // last look before the commit and the merge
  execFileSync("git", ["commit", "-q", "-F", "-"], { cwd: worktree, input: commitMessage(task), env: sessionEnv() });

  run.duration_s = Math.round((Date.now() - started) / 1000);
  if (task.body.hold_before_merge) {
    settleRun(run, "merged");
    log(`  green; held before merge for your review`);
    stage(opts, { task_id: task.id }, "held before merge");
    Object.assign(run, { outcome: "on_hold", diff_stat: git(worktree, "diff", "--stat", `${base}..HEAD`),
      diff: git(worktree, "diff", `${base}..HEAD`).slice(0, 60_000) });
    await context.transition(task.id, "on_hold", run);
    return run;
  }
  if (task.body.after_merge) {
    // Kept for the review after merge: what went in, as it went in.
    Object.assign(run, { diff_stat: git(worktree, "diff", "--stat", `${base}..HEAD`),
      diff: git(worktree, "diff", `${base}..HEAD`).slice(0, 60_000) });
  }
  const r = await integrate(repo, worktree, branch, opts.push, { gates: opts.seams?.gates, paused: isPaused });
  if (r.paused) {
    // Back to uncommitted work on top of the current main, so the resumed session sees its changes.
    tryGit(worktree, "reset", "--soft", "HEAD~1");
    run.base = git(worktree, "rev-parse", "HEAD");
    return pauseTask();
  }
  if (!r.ok) return block(r.reason ?? "could not merge");
  run.outcome = "merged";
  settleRun(run, "merged"); // only now: a merge that fails blocks for its own reason
  run.merged_sha = git(repo, "rev-parse", "main");
  await recordMerged(task, run, opts);
  log(`  merged into main`);
  stage(opts, { task_id: task.id }, `merged into main (${run.merged_sha?.slice(0, 7) ?? ""})`);
  if (task.feature) updateDocs(repo, opts.project, ["--feature", task.feature], log);
  return run;
}

export interface Played {
  id: number;
  title: string;
  outcome: string;
  review_rounds: number;
  duration_s: number;
  tokens: number;
  reason?: string;
}

/** What the runner prints when it stops; the wave page shows the same from the context. */
export function summary(played: Played[]): string {
  if (!played.length) return "";
  const n = (o: string) => played.filter((p) => p.outcome === o).length;
  const total = played.reduce((a, p) => ({ d: a.d + p.duration_s, t: a.t + p.tokens }), { d: 0, t: 0 });
  const mins = (s: number) => `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
  const lines = played.map((p) => `  #${p.id} ${p.outcome.padEnd(8)} ${mins(p.duration_s).padStart(7)}` +
    ` ${String(p.tokens).padStart(8)} tok  review ${p.review_rounds}  ${p.title}` +
    (p.reason ? `\n      ${p.reason.split("\n")[0].slice(0, 160)}` : ""));
  return [`summary: ${n("merged")} merged, ${n("blocked")} blocked, ${n("on_hold")} held` +
    ` · ${mins(total.d)} · ${total.t} tokens`, ...lines].join("\n");
}

async function smokeTestPlan(plan: Plan, opts: RunOptions): Promise<void> {
  const config = (await context.gateConfig(opts.project)).config;
  const cfg = config.smoke_test;
  if (!cfg) return;
  opts.log(`plan #${plan.id} ${plan.body.title}: every task merged; smoke test`);
  stage(opts, { plan_id: plan.id }, "smoke test");
  const r = await smokeTest(plan, cfg, mainCheckout(opts.repo) ?? opts.repo,
    { ...roleModel(config, "tester", opts), language: languageRule(config) }, opts.log,
    live(opts, "tester", { plan_id: plan.id }));
  const { outcome } = await context.smokeTest(plan.id, r);
  opts.log(outcome === "done" ? `  passed; the feature is done`
    : outcome === "fixing" ? `  failed; ${r.fixes.length} fix task(s) queued`
    : `  failed; the plan is blocked for your decision`);
}

/** Runner errors in a row (this one included) that block a task: fewer are infrastructure, not the task. */
export const ERROR_STREAK = 3;

/**
 * An exception of the runner itself (not the usage limit) is not the task's fault: the run is recorded as
 * an error and the task goes back to pending. The third error in a row blocks it, so a bug is not hidden.
 */
export async function recordRunnerError(
  task: Task,
  e: unknown,
  transition: (taskId: number, to: string, run?: Run) => Promise<unknown>,
): Promise<"pending" | "blocked"> {
  const message = String(e);
  let streak = 1;
  for (const r of [...(task.body.runs ?? [])].reverse()) {
    if (r.outcome !== "error") break;
    streak++;
  }
  const blocked = streak >= ERROR_STREAK;
  const reason = blocked ? `runner error (${ERROR_STREAK} in a row): ${message}` : message;
  const to = blocked ? "blocked" : "pending";
  await transition(task.id, to, {
    at: now(), branch: "", worktree: "", base: "", outcome: "error", reason: reason.slice(0, 2000),
  }).catch(() => undefined);
  return to;
}

/** Runs the preflight of a claimed plan and posts the result. Returns false when the plan is held: the task goes
 * back to pending, and when the triage accepts the revision at once the plan is approved, so the next
 * claim runs it in the same run. A failure of the triage leaves the plan held; it never stops the run. */
export async function preflightPlan(plan: Plan, task: Task, opts: RunOptions, seams: {
  preflight?: typeof preflight; ask?: typeof askJev;
} = {}): Promise<boolean> {
  opts.log(`plan #${plan.id} ${plan.body.title}: preflight against main`);
  const r = await (seams.preflight ?? preflight)(plan, opts);
  const { plan_status } = await context.preflight(plan.id, r);
  if (plan_status !== "held") {
    opts.log(`  ready`);
    return true;
  }
  await context.transition(task.id, "pending");
  try {
    const accepted = await triageRevisions(opts.project, (await context.gateConfig(opts.project)).config,
      { ...opts, ask: seams.ask });
    if (accepted.includes(plan.id)) {
      opts.log(`  the revision was accepted by the triage; the plan runs`);
      return false;
    }
  } catch (e) {
    opts.log(`triage: left for you: ${String(e)}`);
  }
  opts.log(`  the plan needs revising; held for your approval`);
  return false;
}

export async function run(opts: RunOptions): Promise<number> {
  if (!(await waitForContext(Number(process.env.AUTONOME_CONTEXT_WAIT_S ?? 30)))) {
    throw new Error("autonome-context is unreachable; the runner does not run tasks without it");
  }
  if (git(opts.repo, "rev-parse", "--show-toplevel") !== resolve(opts.repo)) {
    throw new Error(`${opts.repo} is not the root of a git repository`);
  }
  const checks = await doctor(opts.project, opts.repo);
  const failing = checks.filter((c) => !c.ok);
  if (failing.length) throw new Error(`autonome doctor found problems; not starting:\n${report(failing)}`);
  const warned = checks.filter((c) => c.warn);
  if (warned.length) opts.log(report(warned));
  await replayJournal(opts);
  let ran = 0;
  let sinceOverview = 0;
  const tally: Played[] = [];
  opts.events ??= new EventStream(opts.project);
  try {
    const cfg = (await context.gateConfig(opts.project)).config;
    await triagePending(opts.project, cfg, opts);
    await triageRevisions(opts.project, cfg, opts);
  } catch (e) {
    opts.log(`triage: skipped at start: ${String(e)}`); // never the task's fault; the next run retries
  }
  try {
    return await runAll();
  } finally {
    await opts.events?.flush();
    const text = summary(tally);
    if (text) opts.log(text);
  }

  async function runAll(): Promise<number> {
    let pausedLogged = false;
    while (ran < opts.maxTasks) {
      const { task, preflight: plan, smoke_test: finished, paused } = await context.claim(opts.project);
      if (paused) {
        if (!pausedLogged) opts.log(`queue paused by ${paused.by}${paused.reason ? `: ${paused.reason}` : ""}`);
        pausedLogged = true;
        if (!opts.watch) break;
        await new Promise((r) => setTimeout(r, opts.watch! * 1000));
        continue;
      }
      pausedLogged = false;
      if (finished) {
        await smokeTestPlan(finished, opts);
        continue;
      }
      if (!task) {
        // The queue is empty: a wave may have just finished. Run its retro before waiting or leaving.
        await retroFinishedWaves(opts);
        if (!opts.watch) break;
        if (sinceOverview) updateDocs(opts.repo, opts.project, ["--overview"], opts.log);
        sinceOverview = 0;
        await new Promise((r) => setTimeout(r, opts.watch! * 1000));
        continue;
      }
      try {
        if (plan && !(await preflightPlan(plan, task, opts, opts.seams))) continue;
        const done = await (opts.seams?.runTask ?? runTask)(task, opts);
        if (done) tally.push({ id: task.id, title: task.body.title, outcome: done.outcome, reason: done.reason,
          review_rounds: done.review_rounds ?? 0, duration_s: done.duration_s ?? 0, tokens: done.tokens ?? 0 });
      } catch (e) {
        if (e instanceof MergedUnrecorded) throw e;
        if (e instanceof UsageLimitError || isUsageLimit(String(e))) {
          await context.transition(task.id, "pending", e instanceof UsageLimitError ? e.run : undefined).catch(() => undefined);
          const message = e instanceof Error ? e.message : String(e);
          if (!opts.watch) {
            opts.log(`usage limit: ${message}\n  #${task.id} goes back to pending; run again after the reset`);
            throw e instanceof UsageLimitError ? e : new UsageLimitError(message);
          }
          const wait = untilReset(message);
          opts.log(`usage limit: ${message}\n  #${task.id} goes back to pending; waiting ${Math.round(wait / 60_000)} min for the reset`);
          await new Promise((r) => setTimeout(r, wait));
          continue;
        }
        if (e instanceof ContextError) {
          // Not the task's fault: hand it back and stop, rather than block it or spin on a broken context.
          opts.log(`${e.message}\n  #${task.id} goes back to pending; stopping the runner`);
          await context.transition(task.id, "pending").catch(() => undefined);
          throw e;
        }
        opts.log(`#${task.id} failed unexpectedly: ${String(e)}`);
        tally.push({ id: task.id, title: task.body.title, outcome: "error", reason: String(e), review_rounds: 0, duration_s: 0, tokens: 0 });
        const to = await recordRunnerError(task, e, (id, status, run) => context.transition(id, status, run));
        if (to === "blocked") opts.log(`  #${task.id} failed ${ERROR_STREAK} times in a row; blocked`);
      }
      ran++;
      sinceOverview++;
    }
    if (sinceOverview) updateDocs(opts.repo, opts.project, ["--overview"], opts.log);
    opts.log(ran ? `ran ${ran} task(s)` : "nothing to run");
    return ran;
  }
}

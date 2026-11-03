// The runner: takes approved tasks one by one, runs each in its own session and worktree, and
// only lets work reach main when the gates agree.
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { query } from "./models.js";
import { consult, machineRule } from "./agents.js";
import { killLiveGroups, trackGroup, untrackGroup, withSessionProcess } from "./processes.js";
import { acquireRunLock } from "./runlock.js";
import { CONTEXT_URL, ContextError, context, languageRule, roleModel, sdkModel, waitForContext, type Effort, type GateConfig, type Plan, type Preflight, type ReviewResult, type Run, type Task } from "./context.js";
import { doctor, report } from "./doctor.js";
import { bashPrefixes, scopedFileCommands } from "./fsguard.js";
import { retryMessage, findingsForRetry, gatesSection, recordRound, review, settleRun, type GateOutcome } from "./review.js";
import { smokeTest } from "./smoke-test.js";
import { findJevKey, jevKey, sessionEnv } from "./secrets.js";
import { triagePending, triageRevisions } from "./triage-run.js";
import type { askJev } from "./triage.js";
import { advisoryLine, judgeRuleChange, judgeSatisfied, judgeTaskRevision, raisedLine } from "./triage.js";

/** How much of the delivering commit's diff the Jev sees when a task is closed as already satisfied. */
const SATISFIED_DIFF_CAP = 20_000;
const RULE_DIFF_CAP = 4_000;
import { isUsageLimit, UsageLimitError, untilReset } from "./limits.js";
import { retroFinishedWaves } from "./retro.js";
import { EventStream, fromMessage } from "./events.js";
import { gh as realGh, openPr, prOpenedRun, prState, PrError, type Gh } from "./pr.js";

/** Forwards a session's SDK messages to the live view under a role. */
const live = (opts: RunOptions, role: string, owner: { task_id?: number; plan_id?: number }) =>
  (msg: unknown) => fromMessage(msg).forEach((e) => opts.events?.push({ ...e, role, ...owner }));
const stage = (opts: RunOptions, owner: { task_id?: number; plan_id?: number }, text: string) =>
  opts.events?.push({ role: "runner", kind: "stage", text, ...owner });
import { runStaff, staffEligible } from "./staff.js";
import { applyWip, codeLines, commitMessage, commitWip, fastForwardMain, git, mainCheckout, stageWork, strayFiles, tryGit, wipMessage } from "./git.js";

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
    gates?: JudgeSeams["gates"]; handoff?: typeof writeHandoff; review?: typeof review; staff?: typeof runStaff; commitWip?: typeof commitWip; codeLines?: typeof codeLines;
    gh?: Gh; docs?: (repo: string, project: string, args: string[], log: RunOptions["log"], signal?: AbortSignal) => void | Promise<void> };
  /** The background docs of this run; made on first use. */
  docsQueue?: DocsQueue;
  /** Ceiling for waiting on pending docs before leaving (ms); tests shorten it. */
  docsWaitMs?: number;
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
- {{MACHINE}}
- Copy an Edit's old_string from a Read of those exact lines: tabs, indentation and characters such as
  \`·\` must match, and a guessed string is not found.
- Run tools exactly the way the gates do (e.g. \`uv run pytest\`, \`uv run ruff format .\`), never a bare
  \`python\`, \`pytest\` or \`ruff\`. One plain command per Bash call: no subshells, \`;\` chains or
  \`cd\`. Read and search with the Read, Grep and Glob tools, not \`cat\`, \`grep\` or \`find\`. The gate
  config is \`gate_config_get\` in the autonome memory.
- A command that runs for more than 2 minutes without finishing is investigated, never waited for: run it
  again with a timeout (e.g. \`-x\` and a per-test timeout) to find what hangs, and fix that.
- Nobody will answer questions. If something is truly ambiguous, choose the option most consistent
  with the plan and the conventions, and say so in your final message.
- When done, end your turn with a short summary and, as its last line, \`Commit: <subject>\`: the
  commit subject in Conventional Commits style without the scope (e.g. \`Commit: feat: count clicks per link\`),
  in the language of the project's code and commits, not the \`language\` setting. The gate hook then runs
  every gate and may send you back with a failure (a retry): fix the cause, not the symptom.`;

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
export function implementerPrompt(gc: GateConfig, machine: string = machineRule()): string {
  return IMPLEMENTER_PROMPT.replace("{{SHELL}}", SHELL_RULE[gc.config.permissions === "auto" ? "auto" : "allowlist"])
    .replace("{{MACHINE}}", machine);
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
  human approves it before anything runs.

A task that clearly goes over the task size limit given below (changed lines of code, tests and docs not
counted) is a reason to revise too: split it into tasks that each fit, keeping the same goal and acceptance.`;

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
export function preflightPrompt(plan: Plan, project: string, gates: GateConfig["config"]["gates"], maxLines?: number): string {
  const { title, ...rest } = plan.body;
  delete rest.preflight;
  return `Preflight of plan #${plan.id} (feature ${plan.feature ?? "-"}, project ${project}):\n\n` +
    JSON.stringify({ title, ...rest }, null, 2) +
    `\n\n## Gates it must pass\n${gates.map((g) => `- ${g.name}: \`${g.run}\``).join("\n") || "- (none configured)"}` +
    (maxLines ? `\n\n## Task size limit\nmax_lines: ${maxLines} changed lines of code per task` : "");
}

export async function preflight(plan: Plan, opts: RunOptions): Promise<Preflight> {
  const { gates } = (await context.gateConfig(opts.project)).config;
  const cfg = (await context.gateConfig(opts.project)).config;
  const { answer, cost } = await consult<Preflight>({
    cwd: mainCheckout(opts.repo) ?? opts.repo,
    ...roleModel(cfg, "preflight", opts),
    role: PREFLIGHT_PROMPT,
    language: languageRule(cfg),
    schema: PLAN_SCHEMA,
    prompt: preflightPrompt(plan, opts.project, gates, cfg.task_size?.max_lines),
    onMessage: live(opts, "preflight", { plan_id: plan.id }),
    log: opts.log,
  });
  await context.spend(opts.project, "preflight", cost, { plan_id: plan.id, feature: plan.feature });
  return answer;
}

/** The implementer may also run the commands of gates still pending from approved plans: on a greenfield
 * project the setup task that installs them (e.g. `uv add --dev mypy`) is the first thing to run. */
export function implementerTools(gc: GateConfig): string[] {
  return allowedTools({ ...gc.config, gates: [...gc.config.gates, ...(gc.pending ?? [])] });
}

/** Hard limits that hold in every permissions mode, whatever the classifier thinks. */
export const HARD_DENIALS = [
  ...["commit", "push", "reset", "checkout", "switch", "rebase", "merge", "tag", "branch", "stash", "-C"].map((c) => `Bash(git ${c}:*)`),
];
// A session never waits on a command: Monitor is off, and the fsguard hook denies background Bash.
const NO_WAITING = ["Monitor"];
export const SECRET_DENIALS = ["Read(~/.autonome/**)", "Edit(~/.autonome/**)", "Bash(cat ~/.autonome:*)"];

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

/** The project's gates on the worktree; with `taskId`, the gates that task brings (pending until it merges) too. */
function gates(cwd: string, base: string, taskId?: number): { passed: boolean; summary: string; outcomes?: GateOutcome[] } {
  const r = spawnSync(join(PLUGIN_ROOT, "bin", "autonome-gates"), ["--json", "--base", base], {
    cwd, encoding: "utf8", env: sessionEnv({ AUTONOME_BASE_SHA: base, ...(taskId === undefined ? {} : { AUTONOME_TASK_ID: String(taskId) }) }),
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
async function writeHandoff(sessionId: string, cwd: string, taskId: number, project: string, log: RunOptions["log"],
  base?: string): Promise<void> {
  const transcript = transcriptPath(sessionId);
  if (!transcript) return log(`  no transcript found for ${sessionId}; skipping handoff`);
  const dir = join(AUTONOME_HOME, "logs");
  mkdirSync(dir, { recursive: true });
  const payload = join(dir, `run-${sessionId}.json`);
  writeFileSync(payload, JSON.stringify({ session_id: sessionId, transcript_path: transcript, cwd, task_id: taskId, base }));
  // A child process, not spawnSync: the gates run on this thread while the handoff is written.
  const r = await new Promise<{ status: number | null; stderr: string }>((resolve) => {
    const child = spawn("python3", [join(PLUGIN_ROOT, "hooks", "write_handoff.py"), payload], {
      cwd, env: sessionEnv({ AUTONOME_PROJECT: project }), stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (d) => { stderr = (stderr + d).slice(-2000); });
    child.on("error", (e) => resolve({ status: null, stderr: String(e) }));
    child.on("close", (status) => resolve({ status, stderr }));
  });
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
  let commitSubject: string | undefined;
  let said = "";
  try {
   await withSessionProcess(async (spawnClaudeCodeProcess) => {
    const q = (opts.seams?.query ?? query)({
      prompt: retry?.prompt ?? `Run task #${task.id}: ${task.body.title}. Read the briefing, then implement it.${note ? `\n\n${note}` : ""}`,
      options: {
        resume: retry?.sessionId,
        cwd: worktree,
        ...sdkModel(roleModel(cfg, "implementer", opts)),
        abortController: abort,
        spawnClaudeCodeProcess,
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
        for (const b of msg.message.content) if (b.type === "text") said = (said + "\n" + b.text).slice(-3000);
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
        else {
          const lines = [...(msg.result ?? "").matchAll(/^Commit:\s*(.+)$/gm)];
          if (lines.length) commitSubject = lines[lines.length - 1][1];
        }
      }
    }
   }, opts.log);
  } catch (e) {
    if (e instanceof ContextError) throw e; // not the session's fault, and never a pause
    stop ||= abort.signal.aborted ? `time budget exceeded (${cfg.budget.task_minutes} min)` : String(e);
  } finally {
    clearTimeout(timer);
  }
  return { sessionId, tokens, cost, stop, commitSubject, said } as { sessionId: string; tokens: number; cost: number; stop: string; commitSubject?: string; said?: string };
}

/** The docs agent keeps the living docs true to main after each delivery. A failure does not undo the
 * merge; the doc-drift schedule picks the feature up later. */
export function updateDocs(repo: string, project: string, args: string[], log: RunOptions["log"]): void {
  const cwd = mainCheckout(repo) ?? repo;
  const r = spawnSync(join(PLUGIN_ROOT, "bin", "autonome-docs"), ["--project", project, ...args], { cwd, encoding: "utf8", env: sessionEnv() });
  log(r.status === 0 ? `  docs: ${r.stdout.trim()}` : `  docs update failed: ${r.stderr.slice(-400)}`);
}

/** Like `updateDocs`, but does not block the runner. On abort the whole process group is killed. */
export function updateDocsAsync(repo: string, project: string, args: string[], log: RunOptions["log"], signal?: AbortSignal): Promise<void> {
  const cwd = mainCheckout(repo) ?? repo;
  return new Promise((resolveDocs) => {
    const child = spawn(join(PLUGIN_ROOT, "bin", "autonome-docs"), ["--project", project, ...args], { cwd, env: sessionEnv(), detached: true });
    trackGroup(child.pid);
    let out = "";
    let err = "";
    child.stdout?.on("data", (d) => (out += d));
    child.stderr?.on("data", (d) => (err += d));
    const kill = () => {
      try {
        if (child.pid) process.kill(-child.pid, "SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
    };
    if (signal?.aborted) kill();
    else signal?.addEventListener("abort", kill, { once: true });
    child.on("error", (e) => {
      log(`  docs update failed: ${String(e)}`);
      resolveDocs();
    });
    child.on("close", (code) => {
      signal?.removeEventListener("abort", kill);
      kill(); // nothing the docs agent left behind outlives it
      untrackGroup(child.pid);
      log(code === 0 ? `  docs: ${out.trim()}` : `  docs update failed: ${err.slice(-400)}`);
      resolveDocs();
    });
  });
}

/** The system overview, after a run that played tasks. Like a feature's doc, a failure only logs. */
async function updateOverview(opts: RunOptions): Promise<void> {
  try {
    await (opts.seams?.docs ?? updateDocs)(opts.repo, opts.project, ["--overview"], opts.log);
  } catch (e) {
    opts.log(`  docs update failed: ${String(e)}`);
  }
}

/** Living docs are rewritten once per feature when the queue runs dry, not once per merge: a merge
 * only marks its feature, and `drain` (the queue is empty, or the runner leaves) writes each marked
 * one, one at a time per feature. A merge that lands while its doc runs marks "again after". */
export class DocsQueue {
  private marked = new Set<string>();
  private running = new Map<string, Promise<void>>();
  private again = new Set<string>();
  private abort = new AbortController();
  private closed = false;

  constructor(private opts: RunOptions) {}

  /** A merge into the feature: its doc is written at the next drain. */
  start(feature: string): void {
    if (this.closed) return;
    if (this.running.has(feature)) this.again.add(feature);
    else this.marked.add(feature);
  }

  private launch(feature: string): void {
    if (this.closed) return;
    if (this.running.has(feature)) {
      this.again.add(feature);
      return;
    }
    const docs = this.opts.seams?.docs ?? updateDocsAsync;
    const p = (async () => {
      try {
        await docs(this.opts.repo, this.opts.project, ["--feature", feature], this.opts.log, this.abort.signal);
      } catch (e) {
        this.opts.log(`  docs update failed: ${String(e)}`);
      } finally {
        this.running.delete(feature);
        if (this.again.delete(feature)) this.launch(feature);
      }
    })();
    this.running.set(feature, p);
  }

  /** Writes the marked docs and waits for them up to the ceiling, then kills what is left. */
  async drain(): Promise<void> {
    for (const feature of this.marked) this.launch(feature);
    this.marked.clear();
    if (!this.running.size) return;
    const ceiling = this.opts.docsWaitMs ?? DOCS_WAIT_MS;
    let timer: NodeJS.Timeout | undefined;
    const timedOut = new Promise<"timeout">((r) => (timer = setTimeout(() => r("timeout"), ceiling)));
    const settled = (async () => {
      while (this.running.size) await Promise.all([...this.running.values()]);
      return "done" as const;
    })();
    const result = await Promise.race([settled, timedOut]);
    clearTimeout(timer);
    if (result === "done") return;
    this.closed = true;
    this.again.clear();
    this.abort.abort();
    this.opts.log(`  docs left for the doc-drift schedule`);
    this.running.clear();
  }
}

/** How long the runner waits for pending docs before it leaves, in ms. */
const DOCS_WAIT_MS = 5 * 60_000;

const docsQueue = (opts: RunOptions): DocsQueue => (opts.docsQueue ??= new DocsQueue(opts));

/** Records the task's size on the run; going over the limit is only noted, never a block. */
function measureSize(run: Run, worktree: string, base: string, max: number | undefined, taskId: number, opts: RunOptions): void {
  try {
    run.code_lines = (opts.seams?.codeLines ?? codeLines)(worktree, base);
  } catch (e) {
    opts.log(`  warning: could not measure the task's lines: ${String(e).split("\n")[0]}`);
    return;
  }
  if (max === undefined || run.code_lines <= max) return;
  Object.assign(run, { oversize: true, max_lines: max });
  const text = `task oversize: ${run.code_lines} > ${max}`;
  opts.log(`  ${text}`);
  stage(opts, { task_id: taskId }, text);
}

/** What integrate needs to open a pull request instead of moving main (merge.mode: pr). */
interface PrDelivery {
  mode: "manual" | "auto";
  title: string;
  body: string;
  gh: Gh;
  log: RunOptions["log"];
}

interface Integrated {
  ok: boolean;
  reason?: string;
  paused?: boolean;
  /** gh or the push failed: infrastructure, the task goes back to pending. */
  infra?: boolean;
  pr?: { url: string; mode: "manual" | "auto" };
}

/** Rebase the task's branch on main, re-check the gates, and fast-forward main (or push it and open a pull request). */
async function integrate(repo: string, worktree: string, branch: string, push: boolean,
  hooks: { gates?: JudgeSeams["gates"]; paused?: () => Promise<boolean>; pr?: PrDelivery;
    /** Written while the gates run on top of main; gets the sha where the task's diff starts. Never throws. */
    handoff?: (base: string) => Promise<void>;
    /** After the handoff and the gates, whatever the result (the triage of what the handoff proposed). */
    settle?: () => Promise<void> } = {}): Promise<Integrated> {
  const rebase = tryGit(worktree, "rebase", "main");
  if (!rebase.ok) {
    tryGit(worktree, "rebase", "--abort");
    // Blocked all the same: the handoff explains it, from where the branch leaves main.
    await hooks.handoff?.(git(worktree, "merge-base", "main", "HEAD"));
    await hooks.settle?.();
    return { ok: false, reason: `conflicts with main: ${rebase.out.slice(-800)}` };
  }
  const handoff = hooks.handoff?.(git(worktree, "rev-parse", "main"));
  let check: Awaited<ReturnType<JudgeSeams["gates"]>>;
  try {
    check = await (hooks.gates ?? gates)(worktree, "main");
  } finally {
    await handoff; // the handoff explains a block too, so it finishes before anything else happens
  }
  await hooks.settle?.();
  if (!check.passed) return { ok: false, reason: `gates failed on top of main: ${check.summary}` };
  // The gates took a while: last look before main moves.
  if (await hooks.paused?.()) return { ok: false, paused: true };
  if (hooks.pr) {
    const pushed = tryGit(worktree, "push", "-u", "origin", branch);
    if (!pushed.ok) return { ok: false, infra: true, reason: `could not push ${branch}: ${pushed.out.slice(-400)}` };
    try {
      const pr = openPr(hooks.pr.gh, worktree, { branch, title: hooks.pr.title, body: hooks.pr.body, mode: hooks.pr.mode });
      if (pr.warning) hooks.pr.log(`  warning: ${pr.warning}; ${pr.url} waits for you to merge`);
      tryGit(repo, "worktree", "remove", "--force", worktree); // the branch stays: the pull request is made from it
      return { ok: true, pr: { url: pr.url, mode: pr.mode } };
    } catch (e) {
      if (!(e instanceof PrError)) throw e;
      return { ok: false, infra: true, reason: e.message };
    }
  }
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

/** The worktrees of a task's branches (`autonome/task-<id>-<time>`), oldest first. */
export function taskWorktrees(repo: string, taskId: number): { path: string; branch: string }[] {
  const prefix = `autonome/task-${taskId}-`;
  const found: { path: string; branch: string; time: number }[] = [];
  let path = "";
  for (const line of tryGit(repo, "worktree", "list", "--porcelain").out.split("\n")) {
    if (line.startsWith("worktree ")) path = line.slice(9);
    const branch = line.startsWith("branch refs/heads/") ? line.slice(18) : "";
    if (branch.startsWith(prefix) && existsSync(path)) {
      found.push({ path, branch, time: parseInt(branch.slice(prefix.length), 36) || 0 });
    }
  }
  return found.sort((a, b) => a.time - b.time).map(({ path, branch }) => ({ path, branch }));
}

export const INTERRUPTED = "the runner stopped mid-task";

/** Tasks a runner that died left `running` (the run lock says no other runner is alive): their work is
 * committed as WIP on the branch and they go back to pending, so the next attempt starts from it, as
 * Try again does. A worktree whose WIP cannot be committed stays where it is. */
export async function recoverInterrupted(opts: RunOptions): Promise<void> {
  for (const t of await context.tasksRunning(opts.project)) {
    const wt = taskWorktrees(opts.repo, t.id).at(-1);
    if (!wt) {
      await context.transition(t.id, "pending");
      opts.log(`#${t.id} was left running by a runner that stopped; back to pending (no worktree found)`);
      continue;
    }
    let sha: string | null;
    try {
      sha = (opts.seams?.commitWip ?? commitWip)(wt.path, wipMessage(t.id, INTERRUPTED));
    } catch (e) {
      await context.transition(t.id, "pending");
      opts.log(`#${t.id} was left running; could not commit its WIP, keeping ${wt.path}: ${String(e).split("\n")[0]}`);
      continue;
    }
    const forkPoint = tryGit(wt.path, "merge-base", "main", "HEAD");
    const base = forkPoint.ok ? forkPoint.out : "";
    // Committed already but not merged: that commit is the work.
    const head = tryGit(wt.path, "rev-parse", "HEAD").out;
    sha ??= base && head && head !== base ? head : null;
    await context.transition(t.id, "pending", sha
      ? { at: now(), branch: wt.branch, worktree: wt.path, base, outcome: "interrupted", reason: INTERRUPTED, wip_sha: sha }
      : undefined);
    tryGit(opts.repo, "worktree", "remove", "--force", wt.path);
    if (!sha) tryGit(opts.repo, "branch", "-D", wt.branch);
    opts.log(`#${t.id} was left running by a runner that stopped; back to pending${sha ? `, its work kept as WIP on ${wt.branch}` : ""}`);
  }
}

/** Ctrl-C or a SIGTERM: the sessions and the docs agent run in their own process groups and would
 * outlive the runner, so they are killed first; the task they were on is recovered on the next run. */
function onStopSignals(opts: RunOptions, release: () => void): () => void {
  let stopping = false;
  const handler = (sig: NodeJS.Signals) => {
    if (stopping) process.exit(sig === "SIGINT" ? 130 : 143); // a second Ctrl-C does not wait
    stopping = true;
    opts.log(`${sig}: stopping the runner and its sessions; the task in progress goes back to pending on the next run`);
    void killLiveGroups().finally(() => {
      release();
      process.exit(sig === "SIGINT" ? 130 : 143);
    });
  };
  process.on("SIGINT", handler);
  process.on("SIGTERM", handler);
  return () => { process.off("SIGINT", handler); process.off("SIGTERM", handler); };
}

/** `run` as the CLI starts it: alone on the project (the run lock), after putting back the tasks a runner
 * that died left `running`, and with Ctrl-C killing the sessions instead of leaving them behind. */
export async function runExclusive(opts: RunOptions, home = AUTONOME_HOME, runner: typeof run = run): Promise<number> {
  const release = acquireRunLock(home, opts.project);
  const unhook = onStopSignals(opts, release);
  try {
    if (!(await waitForContext(Number(process.env.AUTONOME_CONTEXT_WAIT_S ?? 30)))) {
      throw new Error("autonome-context is unreachable; the runner does not run tasks without it");
    }
    await recoverInterrupted(opts);
    return await runner(opts);
  } finally {
    unhook();
    release();
  }
}

/** How the task is delivered as a pull request, or undefined while the project merges by fast-forward. */
function prDelivery(task: Task, cfg: GateConfig["config"], opts: RunOptions, subject?: string): PrDelivery | undefined {
  if (cfg.merge?.mode !== "pr") return undefined;
  return {
    mode: cfg.merge.pr, gh: opts.seams?.gh ?? realGh, log: opts.log,
    title: commitMessage(task, subject).split("\n")[0],
    body: `Task #${task.id}: ${CONTEXT_URL}/ui/p/${opts.project}/documents/${task.id}\n\nPlan: #${task.body.plan_id}`,
  };
}

/** The pull request is open: the task waits in `in_pr` until it is merged. */
async function recordPrOpened(task: Task, run: Run, pr: { url: string; mode: "manual" | "auto" }, opts: RunOptions): Promise<Run> {
  const opened = prOpenedRun(run, pr);
  await context.transition(task.id, "in_pr", opened);
  opts.log(`  pull request opened: ${pr.url} (${pr.mode === "auto" ? "GitHub merges when CI passes" : "waiting on you to merge"})`);
  stage(opts, { task_id: task.id }, `pull request opened: ${pr.url}`);
  return opened;
}

/** The push or gh failed: not the task's fault, so it goes back to pending with its work kept on the branch. */
async function recordPrInfra(task: Task, run: Run, reason: string, opts: RunOptions): Promise<Run> {
  opts.log(`  warning: ${reason}; #${task.id} goes back to pending`);
  stage(opts, { task_id: task.id }, `back to pending: ${reason}`);
  const errored: Run = { ...run, outcome: "error", reason: reason.slice(0, 2000) };
  await context.transition(task.id, "pending", errored);
  return errored;
}

/**
 * Looks at each task waiting on its pull request: merged is done (main follows when it is clean), closed
 * is blocked, open is left alone. gh or the network failing logs and tries again; the third failure in a
 * row blocks the task (only the human moves in_pr back to pending), so a broken gh does not hide.
 */
export async function followPrs(opts: RunOptions): Promise<void> {
  const run = opts.seams?.gh ?? realGh;
  let waiting: Awaited<ReturnType<typeof context.tasksInPr>>;
  try {
    const prMode = (await context.gateConfig(opts.project)).config.merge?.mode === "pr";
    waiting = await context.tasksInPr(opts.project);
    if (!prMode) {
      // Not followed outside pr mode (no gh needed), but not silent either: the task waits until the mode is pr again.
      if (waiting.length) opts.log(`warning: ${waiting.length} task(s) in_pr (${waiting.map((t) => `#${t.id}`).join(", ")}) but merge.mode is not pr; their pull requests are not followed`);
      return;
    }
  } catch (e) {
    if (e instanceof ContextError) return void opts.log(`warning: could not list the pull requests: ${e.message}`);
    throw e;
  }
  for (const t of waiting) {
    try {
      const pr = prState(run, opts.repo, t.pr_url);
      if (pr.state === "OPEN") {
        prFailures.delete(t.id);
        continue;
      }
      const base: Run = { at: now(), branch: "", worktree: "", base: "", outcome: "", pr_url: t.pr_url };
      if (pr.state === "CLOSED") {
        opts.log(`#${t.id} ${t.pr_url} was closed without merging; blocked`);
        await context.transition(t.id, "blocked", { ...base, outcome: "blocked", reason: "PR closed without merge" });
        continue;
      }
      if (!pr.mergeSha) throw new PrError(`${t.pr_url} is merged but gh gave no merge commit`);
      const fetched = tryGit(opts.repo, "fetch", "origin", "main");
      const ff = fetched.ok ? fastForwardMain(opts.repo, "origin/main") : { ok: false, reason: fetched.out };
      if (!ff.ok) opts.log(`  warning: main was not brought up to date: ${(ff.reason ?? "").split("\n")[0]}`);
      await context.transition(t.id, "done", { ...base, outcome: "merged", merged_sha: pr.mergeSha });
      opts.log(`#${t.id} ${t.pr_url} was merged (${pr.mergeSha.slice(0, 7)})`);
      stage(opts, { task_id: t.id }, `pull request merged (${pr.mergeSha.slice(0, 7)})`);
      if (t.feature) docsQueue(opts).start(t.feature);
      prFailures.delete(t.id);
    } catch (e) {
      if (!(e instanceof PrError || e instanceof ContextError)) throw e;
      if (e instanceof ContextError) {
        opts.log(`warning: #${t.id}: ${e.message}`); // the context is down: looked at again on the next turn
        continue;
      }
      const streak = (prFailures.get(t.id) ?? 0) + 1;
      prFailures.set(t.id, streak);
      if (streak < ERROR_STREAK) {
        opts.log(`warning: #${t.id}: ${e.message}`); // looked at again on the next turn of the loop
        continue;
      }
      // Not hidden any longer: in_pr only goes back to pending by the human, so it blocks, work kept on the PR.
      // The streak is cleared only once the task is blocked: if the context is down, the next turn blocks it again.
      opts.log(`#${t.id} ${t.pr_url}: gh failed ${ERROR_STREAK} times in a row; blocked`);
      await context.transition(t.id, "blocked", {
        at: now(), branch: "", worktree: "", base: "", outcome: "blocked", pr_url: t.pr_url,
        reason: `could not follow the pull request (${ERROR_STREAK} in a row): ${e.message}`.slice(0, 2000),
      }).then(() => void prFailures.delete(t.id), (te) => {
        if (!(te instanceof ContextError)) throw te;
        opts.log(`warning: #${t.id}: ${te.message}`);
      });
    }
  }
}

/** Consecutive failures to look at each task's pull request (gh or the network), per process. */
const prFailures = new Map<number, number>();
export const resetPrFailures = (): void => prFailures.clear();

/** The reviewer's verdict for the live view: the verdict, then one line per finding. */
export function verdictLine(r: ReviewResult): string {
  if (!r.findings.length) return `**${r.verdict}**, no findings`;
  const n = r.findings.length;
  return `**${r.verdict}**, ${n} finding${n > 1 ? "s" : ""}:\n` + r.findings.map((f) =>
    `- ${f.severity} ${f.kind}${f.file ? ` in \`${f.file}\`` : ""}: ${f.issue.replace(/\s+/g, " ")}`).join("\n");
}

/** What `judgeTask` calls out to; tests swap them (a reviewer that fails round 1 and passes round 2). */
type GateResult = { passed: boolean; summary: string; outcomes?: GateOutcome[] };

export interface JudgeSeams {
  gates: (cwd: string, base: string, taskId?: number) => GateResult | Promise<GateResult>;
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
    const check = await seams.gates(worktree, base);
    stage(opts, { task_id: task.id }, check.passed ? "gates passed" : `gates: ${check.summary.split(":")[0]}`);
    if (!check.passed) return { failure: check.summary, blockedOnReview: false };
    if (!cfg.review.enabled) return { failure: null, blockedOnReview: false };
    if (await input.paused?.()) return { failure: null, blockedOnReview: false, paused: true };
    const briefing = [await seams.briefing(), gatesSection(check.outcomes)].filter(Boolean).join("\n\n");
    stage(opts, { task_id: task.id }, `review, round ${round + 1}`);
    const key = cfg.triage?.model === "jev-latest" ? jevKey((l) => opts.log(`triage: ${l}`)) : undefined;
    const rv = await seams.review(task, briefing, worktree, base,
      { ...roleModel(cfg, "reviewer", opts), language: languageRule(cfg) },
      live(opts, "reviewer", { task_id: task.id }), run.review?.findings, { cfg: cfg.triage, key }, opts.log);
    recordRound(run, rv.result, round + 1);
    opts.events?.push({ task_id: task.id, role: "reviewer", kind: "say", text: verdictLine(rv.result) });
    const checked = advisoryLine(rv.result.findings, cfg.triage?.model === "jev-latest" && !key ? "no Jev key" : undefined);
    for (const line of [checked, raisedLine(rv.result.findings)]) {
      if (!line) continue;
      log(`  ${line}`);
      opts.events?.push({ task_id: task.id, role: "triage", kind: "say", text: line });
    }
    run.cost_usd = (run.cost_usd ?? 0) + rv.cost;
    // Medium findings go back once when the project asks for it (`retry_medium`); otherwise only high
    // ones do, and the medium ones become loose ends at merge, so a pass costs no extra round.
    const message = retryMessage(rv.result, !!cfg.review.retry_medium && round < cfg.review.max_rounds && !mediumSent);
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
    const pr = prDelivery(task, (await context.gateConfig(opts.project)).config, opts);
    const r = await integrate(repo, last.worktree, last.branch, opts.push, {
      gates: opts.seams?.gates, pr, paused: async () => !!(await context.projectPause(opts.project)) });
    if (r.paused) {
      // Nothing was pushed or merged; the task stays released and the next run takes it up.
      log(`  queue paused; #${task.id} stays released, nothing merged or pushed`);
      return undefined;
    }
    if (r.pr) return recordPrOpened(task, { ...last, at: now() }, r.pr, opts);
    if (r.infra) return recordPrInfra(task, { ...last, at: now() }, r.reason ?? "gh failed", opts);
    const final: Run = { ...last, at: now(), outcome: r.ok ? "merged" : "merge-failed", reason: r.reason };
    if (r.ok) await recordMerged(task, final, opts);
    else await context.transition(task.id, "pending", final);
    log(r.ok ? `  merged` : `  could not merge: ${r.reason}`);
    if (r.ok && task.feature) docsQueue(opts).start(task.feature);
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
  let lastGates = "";
  const judge = () => judgeTask({ task, cfg, opts, run, worktree, base, current: () => result, retry, paused: isPaused }, {
    gates: async (cwd, b) => {
      const check = await (opts.seams?.gates ?? gates)(cwd, b, task.id);
      lastGates = check.summary;
      return check;
    },
    review: opts.seams?.review ?? review,
    briefing: async () => (await context.briefing(opts.project, task.id)).text + ruleNote,
  });
  // A rule change the staff made and the Jev accepted, told to the reviewer so it is not taken for a weakened gate.
  let ruleNote = "";
  // The staff looks once, from outside, before the task blocks; what it fixes goes back through the gates and the reviewer.
  const staffStep = async (why: string): Promise<{ failure: string | null; paused?: boolean; closed?: boolean }> => {
    const gc = await context.gateConfig(opts.project);
    const model = roleModel(cfg, "staff", opts);
    const briefingText = (await context.briefing(opts.project, task.id)).text;
    stage(opts, { task_id: task.id }, "staff: looking");
    stageWork(worktree);
    let ran: Awaited<ReturnType<typeof runStaff>>;
    try {
      ran = await (opts.seams?.staff ?? runStaff)({
        input: { task, failure: why, briefing: briefingText, gates: lastGates,
          findings: blockedOnReview && run.review ? findingsForRetry(run.review) : undefined, said: result.said, repo, worktree, base },
        model, permissions: implementerPermissions(gc, worktree), language: languageRule(cfg), pluginRoot: PLUGIN_ROOT,
        env: { AUTONOME_RUN: "1", AUTONOME_TASK_ID: String(task.id), AUTONOME_BASE_SHA: base, AUTONOME_PROJECT: opts.project,
          BASH_DEFAULT_TIMEOUT_MS: "120000", BASH_MAX_TIMEOUT_MS: "300000", PATH: `${join(PLUGIN_ROOT, "bin")}:${process.env.PATH}` },
        timeoutMs: cfg.budget.task_minutes * 60_000, onMessage: live(opts, "staff", { task_id: task.id }), log, query: opts.seams?.query,
      });
    } catch (e) {
      if (e instanceof ContextError || e instanceof UsageLimitError || isUsageLimit(String(e))) throw e;
      const err = (e instanceof Error ? e.message : String(e)).split("\n")[0];
      run.staff = { action: "failed", diagnosis: err, model: model.model, cost_usd: 0 };
      stage(opts, { task_id: task.id }, "staff: failed");
      log(`  staff failed: ${err}`);
      return { failure: `${why}\nstaff failed: ${err}` };
    }
    const { answer } = ran;
    run.staff = { action: answer.action, diagnosis: answer.diagnosis, model: ran.model, cost_usd: ran.cost };
    run.cost_usd = (run.cost_usd ?? 0) + ran.cost;
    await context.spend(opts.project, "staff", ran.cost, { task_id: task.id, feature: task.feature });
    stage(opts, { task_id: task.id }, `staff: ${answer.action}`);
    log(`  staff: ${answer.action}: ${answer.diagnosis.split("\n")[0]}`);
    const blocked = `staff: ${answer.diagnosis}`;
    // The Jev judges what closes a task or changes its spec; no key, no triage or a failed call never decides.
    const jevJudge = async <T>(what: (key: string) => Promise<T>): Promise<T | null> => {
      const key = cfg.triage?.model === "jev-latest" ? jevKey((l) => log(`  triage: ${l}`)) : undefined;
      if (!key) return null;
      try {
        return await what(key);
      } catch (e) {
        if (e instanceof UsageLimitError || isUsageLimit(String(e))) throw e;
        log(`  jev failed: ${(e instanceof Error ? e.message : String(e)).split("\n")[0]}`);
        return null;
      }
    };
    const flagged = (j: { answers: Record<string, number> }) => {
      const [flag, value] = Object.entries(j.answers).sort((a, b) => b[1] - a[1])[0];
      return `${flag} ${value}`;
    };
    const ask = opts.seams?.ask;
    if (answer.action === "already_satisfied") {
      const sha = answer.satisfied_by ?? "";
      const dirty = tryGit(worktree, "status", "--porcelain");
      if (!/^[0-9a-f]{7,40}$/i.test(sha) || !tryGit(repo, "merge-base", "--is-ancestor", sha, "main").ok) {
        return { failure: `${blocked}\n(satisfied_by ${sha || "(none)"} is not a commit in main)` };
      }
      if (!dirty.ok || dirty.out) return { failure: `${blocked}\n(the worktree has changes; not closed as already satisfied)` };
      const shown = tryGit(repo, "show", "--stat", "--patch", sha);
      if (!shown.ok) return { failure: blocked };
      const { title, spec, acceptance } = task.body;
      const j = cfg.triage && await jevJudge((key) => judgeSatisfied({ task: { title, spec, acceptance },
        delivered: shown.out.slice(0, SATISFIED_DIFF_CAP) }, cfg.triage!, key, ask));
      if (!j) return { failure: blocked };
      if (j.verdict === "hold") return { failure: `${blocked} (Jev: ${flagged(j)})` };
      settleRun(run, undefined);
      Object.assign(run, { outcome: "already_satisfied", satisfied_by: sha,
        satisfied_triage: { model: j.model, answers: Object.fromEntries(Object.entries(j.answers).map(([k, v]) => [k, String(v)])), verdict: "accept" },
        duration_s: Math.round((Date.now() - started) / 1000) });
      stage(opts, { task_id: task.id }, `already satisfied by ${sha}`);
      log(`  already satisfied by ${sha}: closed without a merge`);
      await context.transition(task.id, "done", run);
      tryGit(repo, "worktree", "remove", "--force", worktree);
      tryGit(repo, "branch", "-D", branch);
      return { failure: null, closed: true };
    }
    if (answer.action === "revise") {
      const revised = answer.revised;
      if (!revised) return { failure: blocked };
      const { title, spec, acceptance } = task.body;
      const j = cfg.triage && await jevJudge((key) => judgeTaskRevision({ original: { title, spec, acceptance }, revised,
        plan: briefingText }, cfg.triage!, key, ask));
      if (!j) return { failure: blocked };
      try {
        await context.staffRevision(task.id, { revised, diagnosis: answer.diagnosis, model: j.model, verdict: j.verdict,
          answers: Object.fromEntries(Object.entries(j.answers).map(([k, v]) => [k, String(v)])) });
      } catch (e) {
        if (e instanceof ContextError) throw e;
        log(`  staff revision not recorded: ${String(e).split("\n")[0]}`);
        return { failure: blocked };
      }
      if (j.verdict === "hold") return { failure: `${blocked} (Jev: ${flagged(j)})` };
      Object.assign(task.body, revised);
      stage(opts, { task_id: task.id }, "spec revised by staff");
      log(`  spec revised by staff`);
      result = { sessionId: ran.sessionId, tokens: 0, cost: 0, stop: "", commitSubject: result.commitSubject };
      run.session_id = ran.sessionId || run.session_id;
      await retry(`implement the revised task\n\n# ${revised.title}\n${revised.spec}\n\nAcceptance:\n${revised.acceptance.map((a) => `- ${a}`).join("\n")}`);
      const after = await judge();
      if (after.paused) return { failure: null, paused: true };
      blockedOnReview = after.blockedOnReview;
      return { failure: after.failure && `${blocked}\n${after.failure}` };
    }
    if (answer.action === "fix_rule") {
      const rc = answer.rule_change;
      const files = rc?.files ?? [];
      const inside = files.every((f) => !isAbsolute(f) && !f.split(/[\\/]/).includes("..")
        && tryGit(worktree, "cat-file", "-e", `${base}:${f}`).ok);
      const diff = rc && inside ? tryGit(worktree, "diff", base, "--", ...files) : { ok: false, out: "" };
      if (!rc || !inside || !diff.ok || !diff.out) {
        return { failure: `${blocked}\n(fix_rule without a change to rule files that exist on the base)` };
      }
      const proposal = `\n\nProposed rule change (undone; apply it to unblock):\n${diff.out.slice(0, RULE_DIFF_CAP)}`;
      const undo = () => { tryGit(worktree, "checkout", base, "--", ...files); };
      const j = cfg.triage && await jevJudge((key) => judgeRuleChange({ task: task.body, plan: briefingText, why: rc.why,
        gates: lastGates.slice(0, 6000), rule_diff: diff.out.slice(0, RULE_DIFF_CAP) }, cfg.triage!, key, ask));
      run.staff.rule_change = { files, why: rc.why, diff: diff.out.slice(0, RULE_DIFF_CAP), verdict: j ? j.verdict : "none",
        answers: j ? Object.fromEntries(Object.entries(j.answers).map(([k, v]) => [k, String(v)])) : {} };
      if (!j) { undo(); return { failure: `${blocked}${proposal}` }; }
      if (j.verdict === "hold") { undo(); return { failure: `${blocked} (Jev: ${flagged(j)})${proposal}` }; }
      stage(opts, { task_id: task.id }, `rule changed by staff: ${files.join(", ")} (Jev: accept)`);
      log(`  rule changed by staff: ${files.join(", ")}`);
      ruleNote = `\n\n## Rule changed by the staff\nThe staff changed ${files.join(", ")} because the rule, not the code, went `
        + `against the plan: ${rc.why}\nThe Jev judged that it weakens nothing the plan or a convention forbids. `
        + `Do not report this change as a weakened gate.`;
    } else if (answer.action !== "fixed") return { failure: blocked };
    result = { sessionId: ran.sessionId, tokens: 0, cost: ran.cost, stop: "", commitSubject: result.commitSubject };
    run.session_id = ran.sessionId || run.session_id;
    const again = await judge();
    if (again.paused) return { failure: null, paused: true };
    blockedOnReview = again.blockedOnReview;
    return { failure: again.failure && `staff: ${answer.diagnosis}\n${again.failure}` };
  };
  let failure: string | null;
  try {
    const judged = await judge();
    if (judged.paused) return pauseTask();
    failure = judged.failure;
    blockedOnReview = judged.blockedOnReview;
    if (staffEligible(failure, run)) {
      if (await isPaused()) return pauseTask(); // the staff does not start on a paused queue
      const out = await staffStep(failure);
      if (out.paused) return pauseTask();
      if (out.closed) return run;
      failure = out.failure;
    }
  } catch (e) {
    // Out of usage (in the implementer, a reviewer or the staff): the work so far is kept as WIP, the task runs again later.
    if (e instanceof UsageLimitError || isUsageLimit(String(e))) throw onUsageLimit(e);
    throw e;
  }
  if (!failure && (await isPaused())) return pauseTask(); // before the handoff: a paused session is not finished
  const pr = prDelivery(task, cfg, opts, result.commitSubject);
  // On the way to a fast-forward the handoff is written while integrate's gates run; elsewhere it comes first.
  const alongGates = !failure && !!result.sessionId && !task.body.hold_before_merge && !pr;
  const handoff = async (since?: string) => {
    if (!result.sessionId) return;
    try {
      await (opts.seams?.handoff ?? writeHandoff)(result.sessionId, worktree, task.id, opts.project, log, since);
    } catch (e) {
      log(`  handoff failed: ${String(e).split("\n")[0]}`);
    }
  };
  const settle = () => triagePending(opts.project, cfg, opts);
  if (!alongGates) {
    await handoff();
    if (result.sessionId) await settle();
  }
  if (failure) return block(failure);
  if (await isPaused()) return pauseTask(); // last look before the commit and the merge
  execFileSync("git", ["commit", "-q", "-F", "-"], { cwd: worktree, input: commitMessage(task, result.commitSubject), env: sessionEnv() });

  run.duration_s = Math.round((Date.now() - started) / 1000);
  measureSize(run, worktree, base, cfg.task_size?.max_lines, task.id, opts);
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
  const r = await integrate(repo, worktree, branch, opts.push, { gates: opts.seams?.gates, paused: isPaused, pr,
    handoff: alongGates ? handoff : undefined, settle: alongGates ? settle : undefined });
  if (r.paused) {
    // Back to uncommitted work on top of the current main, so the resumed session sees its changes.
    tryGit(worktree, "reset", "--soft", "HEAD~1");
    run.base = git(worktree, "rev-parse", "HEAD");
    return pauseTask();
  }
  if (r.pr) {
    settleRun(run, "merged");
    run.duration_s = Math.round((Date.now() - started) / 1000);
    return recordPrOpened(task, run, r.pr, opts);
  }
  if (r.infra) {
    // Pushed or not, the work is committed on the branch: Try again starts from it.
    Object.assign(run, { outcome: "error", reason: r.reason, duration_s: Math.round((Date.now() - started) / 1000) });
    const wip = keepWork(r.reason ?? "gh failed");
    if (wip.sha) run.wip_sha = wip.sha;
    const done = await recordPrInfra(task, run, r.reason ?? "gh failed", opts);
    if (wip.ok) tryGit(repo, "worktree", "remove", "--force", worktree);
    return done;
  }
  if (!r.ok) return block(r.reason ?? "could not merge");
  run.outcome = "merged";
  settleRun(run, "merged"); // only now: a merge that fails blocks for its own reason
  run.merged_sha = git(repo, "rev-parse", "main");
  await recordMerged(task, run, opts);
  log(`  merged into main`);
  stage(opts, { task_id: task.id }, `merged into main (${run.merged_sha?.slice(0, 7) ?? ""})`);
  if (task.feature) docsQueue(opts).start(task.feature);
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
  const gc = await context.gateConfig(opts.project);
  const config = gc.config;
  const cfg = config.smoke_test;
  if (!cfg) return;
  opts.log(`plan #${plan.id} ${plan.body.title}: every task merged; smoke test`);
  stage(opts, { plan_id: plan.id }, "smoke test");
  const r = await smokeTest(plan, cfg, mainCheckout(opts.repo) ?? opts.repo,
    { ...roleModel(config, "tester", opts), language: languageRule(config) }, opts.log,
    live(opts, "tester", { plan_id: plan.id }), { gc });
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
  const checks = await doctor(opts.project, opts.repo, { gh: opts.seams?.gh });
  const failing = checks.filter((c) => !c.ok);
  if (failing.length) throw new Error(`autonome doctor found problems; not starting:\n${report(failing)}`);
  const warned = checks.filter((c) => c.warn);
  if (warned.length) opts.log(report(warned));
  try {
    await context.runnerStatus(opts.project, Boolean(findJevKey().key));
  } catch (e) {
    opts.log(`warning: could not report the Jev key status to the context: ${String(e)}`); // never the task's fault
  }
  await replayJournal(opts);
  await followPrs(opts);
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
    await docsQueue(opts).drain();
    await opts.events?.flush();
    const text = summary(tally);
    if (text) opts.log(text);
  }

  async function runAll(): Promise<number> {
    let pausedLogged = false;
    let firstTurn = true;
    while (ran < opts.maxTasks) {
      if (!firstTurn) await followPrs(opts); // the start of the run did it for the first turn
      firstTurn = false;
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
        if (sinceOverview) {
          await docsQueue(opts).drain();
          await updateOverview(opts);
        }
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
    if (sinceOverview) {
      await docsQueue(opts).drain();
      await updateOverview(opts);
    }
    opts.log(ran ? `ran ${ran} task(s)` : "nothing to run");
    return ran;
  }
}

// The maestro: takes approved tasks one by one, plays each in its own session and worktree, and
// only lets work reach main when the gates agree.
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { consult } from "./agents.js";
import { ContextError, context, languageRule, roleModel, waitForContext, type Effort, type GateConfig, type Plan, type Rehearsal, type ReviewResult, type Run, type Task } from "./context.js";
import { doctor, report } from "./doctor.js";
import { bashPrefixes, scopedFileCommands } from "./fsguard.js";
import { encoreMessage, findingsForEncore, gatesSection, recordRound, review, settleRun, type GateOutcome } from "./review.js";
import { soundcheck } from "./soundcheck.js";
import { jevKey, sessionEnv } from "./secrets.js";
import { triagePending } from "./triage-run.js";
import { isUsageLimit, UsageLimitError, untilReset } from "./limits.js";
import { tuneFinishedWaves } from "./tune.js";
import { EventStream, fromMessage } from "./events.js";

/** Forwards a session's SDK messages to the live view under a role. */
const live = (opts: ConductOptions, role: string, owner: { task_id?: number; plan_id?: number }) =>
  (msg: unknown) => fromMessage(msg).forEach((e) => opts.events?.push({ ...e, role, ...owner }));
const stage = (opts: ConductOptions, owner: { task_id?: number; plan_id?: number }, text: string) =>
  opts.events?.push({ role: "maestro", kind: "stage", text, ...owner });
import { commitMessage, fastForwardMain, git, mainCheckout, stageWork, strayFiles, tryGit } from "./git.js";

export const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export const AUTONOME_HOME = process.env.AUTONOME_HOME ?? join(homedir(), ".autonome");

export interface ConductOptions {
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
  /** Where the live view's events go; conduct makes one when none is given. */
  events?: EventStream;
}

const MUSICIAN_PROMPT = `You are the implementer of one task in the autonome process. The briefing injected at
the start of this session has the task, its plan, the conventions for its layers and the gates.

- Do exactly the task. Nothing listed as out of scope, no drive-by refactors.
- Prove each acceptance criterion with a test or a gate where possible.
- While working, run the fast gates with \`autonome-gates --fast\`.
- Do not commit, push, switch branches or touch git history: the runner does that. It commits every
  file you leave in the tree that .gitignore does not exclude; to keep a generated file (coverage data,
  build output, a local database) out, add it to .gitignore.
- Never edit, weaken or skip a gate or the rules behind it to make it pass.
- Create and change files only with the Write and Edit tools. The shell is for running things: the
  gates' commands, \`autonome-gates --fast\`, read-only git and inspection. Writing through the shell
  (heredocs, \`cat >\`, \`python -c\`, \`sed -i\`) is denied in this session, and so is \`git -C\`:
  you are already in the right directory.
- Run tools exactly the way the gates do (e.g. \`uv run pytest\`, \`uv run ruff format .\`), never a bare
  \`python\`, \`pytest\` or \`ruff\`. One plain command per Bash call: no subshells, \`;\` chains or
  \`cd\`. Read and search with the Read, Grep and Glob tools, not \`cat\`, \`grep\` or \`find\`. The gate
  config is \`gate_config_get\` in the autonome memory.
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
    // git refuses paths outside the repository, so these stay inside the worktree on their own.
    "Bash(git rm:*)", "Bash(git mv:*)", "Bash(git update-index:*)", "Bash(git add:*)", "Bash(git check-ignore:*)",
    "Bash(git ls-files:*)", "Bash(git rev-parse:*)", "Bash(git merge-base:*)",
    // Read-only looks around: what is installed, what a jar holds (find goes through the hook).
    "Bash(ls:*)", "Bash(which:*)", "Bash(unzip -l:*)", "Bash(jar tf:*)",
    // Read-only helpers sessions reach for; writing goes through Edit/Write, never the shell.
    "Bash(sed -n:*)", "Bash(echo:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    ...[...commands].map((c) => `Bash(${c}:*)`),
    // The memory, under the plugin's server name (the musician loads the plugin) and the plain one.
    ...["search", "conventions_get", "plan_get", "gate_config_get", "history_get"].flatMap((t) => [`mcp__autonome__${t}`, `mcp__plugin_autonome_autonome__${t}`]),
  ];
}

export const REHEARSAL_PROMPT = `You rehearse a plan of the autonome process before it plays. It was written and
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
  human approves it before anything plays.`;

export const PLAN_SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["ready", "revise"] },
    notes: { type: "string" },
    plan: { type: "object", description: "the full revised plan, only when the verdict is revise" },
  },
  required: ["verdict", "notes"],
} as const;

/** The rehearsal: a read-only session checks a wave plan against main before its first task plays. */
export function rehearsalPrompt(plan: Plan, project: string, gates: GateConfig["config"]["gates"]): string {
  const { title, ...rest } = plan.body;
  delete rest.rehearsal;
  return `Rehearse plan #${plan.id} (feature ${plan.feature ?? "-"}, project ${project}):\n\n` +
    JSON.stringify({ title, ...rest }, null, 2) +
    `\n\n## Gates it must pass\n${gates.map((g) => `- ${g.name}: \`${g.run}\``).join("\n") || "- (none configured)"}`;
}

export async function rehearse(plan: Plan, opts: ConductOptions): Promise<Rehearsal> {
  const { gates } = (await context.gateConfig(opts.project)).config;
  const cfg = (await context.gateConfig(opts.project)).config;
  const { answer } = await consult<Rehearsal>({
    cwd: mainCheckout(opts.repo) ?? opts.repo,
    ...roleModel(cfg, "rehearsal", opts),
    role: REHEARSAL_PROMPT,
    language: languageRule(cfg),
    schema: PLAN_SCHEMA,
    prompt: rehearsalPrompt(plan, opts.project, gates),
    onMessage: live(opts, "rehearsal", { plan_id: plan.id }),
  });
  return answer;
}

/** The musician may also run the commands of gates still pending from approved plans: on a greenfield
 * project the setup task that installs them (e.g. `uv add --dev mypy`) is the first thing to play. */
export function musicianTools(gc: GateConfig): string[] {
  return allowedTools({ ...gc.config, gates: [...gc.config.gates, ...(gc.pending ?? [])] });
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
function writeHandoff(sessionId: string, cwd: string, taskId: number, project: string, log: ConductOptions["log"]): void {
  const transcript = transcriptPath(sessionId);
  if (!transcript) return log(`  no transcript found for ${sessionId}; skipping handoff`);
  const dir = join(AUTONOME_HOME, "logs");
  mkdirSync(dir, { recursive: true });
  const payload = join(dir, `conduct-${sessionId}.json`);
  writeFileSync(payload, JSON.stringify({ session_id: sessionId, transcript_path: transcript, cwd, task_id: taskId }));
  const r = spawnSync("python3", [join(PLUGIN_ROOT, "hooks", "write_handoff.py"), payload], {
    cwd, encoding: "utf8", env: sessionEnv({ AUTONOME_PROJECT: project }) });
  log(r.status === 0 ? `  handoff written` : `  handoff failed: ${r.stderr.slice(-500)}`);
}

/** One session of the musician; `encore` resumes it with what to fix. */
async function play(task: Task, worktree: string, base: string, opts: ConductOptions,
  encore?: { sessionId: string; prompt: string }) {
  const gateConfig = await context.gateConfig(opts.project);
  const cfg = gateConfig.config;
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
        ...roleModel(cfg, "musician", opts),
        abortController: abort,
        permissionMode: "dontAsk",
        allowedTools: musicianTools(gateConfig),
        hooks: { PreToolUse: [scopedFileCommands(worktree, bashPrefixes(musicianTools(gateConfig)))] },
        settingSources: ["project"],
        plugins: [{ type: "local", path: PLUGIN_ROOT }],
        systemPrompt: { type: "preset", preset: "claude_code", append: MUSICIAN_PROMPT },
        env: sessionEnv({
          AUTONOME_CONDUCT: "1",
          AUTONOME_TASK_ID: String(task.id),
          AUTONOME_BASE_SHA: base,
          AUTONOME_PROJECT: opts.project,
          PATH: `${join(PLUGIN_ROOT, "bin")}:${process.env.PATH}`,
        }),
      },
    })) {
      if (msg.type === "system" && msg.subtype === "init") sessionId = msg.session_id;
      live(opts, "musician", { task_id: task.id })(msg);
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
  const r = spawnSync(join(PLUGIN_ROOT, "bin", "autonome-docs"), ["--project", project, ...args], { cwd, encoding: "utf8", env: sessionEnv() });
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

/** Thrown when a task reached main but the context could not record it; the next conduct does. */
class MergedUnrecorded extends ContextError {}

function journalPath(project: string, taskId: number): string {
  return join(AUTONOME_HOME, "state", `merged-${project}-${taskId}.json`);
}

/** Main moved: the context must learn it, now or on the next conduct, so the task never plays twice. */
async function recordMerged(task: Task, run: Run, opts: ConductOptions): Promise<void> {
  try {
    await context.transition(task.id, "done", run);
  } catch (e) {
    if (!(e instanceof ContextError)) throw e;
    mkdirSync(join(AUTONOME_HOME, "state"), { recursive: true });
    writeFileSync(journalPath(opts.project, task.id), JSON.stringify({ task_id: task.id, run }));
    throw new MergedUnrecorded(`${e.message}\n  #${task.id} is merged into main; it will be recorded as done on the next conduct`);
  }
}

/** Records merges an earlier conduct could not report. */
async function replayJournal(opts: ConductOptions): Promise<void> {
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
  opts: ConductOptions;
  run: Run;
  worktree: string;
  base: string;
  /** The musician's latest session; changes after each encore. */
  current: () => { sessionId: string; stop: string };
  encore: (prompt: string) => Promise<void>;
}

/** Gates, then the reviewer; blocking findings send the musician back to the same session.
 * Returns why the task cannot merge (null when it can) and whether the reviewer's findings are the reason. */
export async function judgeTask(input: JudgeInput, seams: JudgeSeams): Promise<{ failure: string | null; blockedOnReview: boolean }> {
  const { task, cfg, opts, run, worktree, base, encore } = input;
  const log = opts.log;
  let cleanups = 0;
  let mediumSent = false;
  for (let round = 0; ; ) {
    const result = input.current();
    const state = result.sessionId ? metronomeState(result.sessionId) : {};
    if (result.stop && isUsageLimit(result.stop)) throw new UsageLimitError(result.stop);
    if (result.stop) return { failure: result.stop, blockedOnReview: false };
    if (state.status === "blocked") return { failure: `metronome: ${state.reason}`, blockedOnReview: false };
    stageWork(worktree);
    if (!git(worktree, "status", "--porcelain")) return { failure: "the session changed nothing", blockedOnReview: false };
    // Built in, whatever the project's gates: nothing ignored or unasked-for binary gets committed.
    const stray = strayFiles(worktree, base, `${task.body.title}\n${task.body.spec}`);
    if (stray.length) {
      const list = stray.map((p) => `- ${p}`).join("\n");
      if (cleanups >= cfg.encore.max) return { failure: `stray files still in the change:\n${list}`, blockedOnReview: false };
      cleanups++;
      log(`  stray files in the change; encore ${cleanups}`);
      stage(opts, { task_id: task.id }, `encore: stray files in the change`);
      await encore(`These files would be committed but do not belong in the change. Add them (or their ` +
        `pattern) to .gitignore; the runner restages from scratch, so ignoring them is enough:\n${list}`);
      continue;
    }
    stage(opts, { task_id: task.id }, "gates");
    const check = seams.gates(worktree, base);
    stage(opts, { task_id: task.id }, check.passed ? "gates passed" : `gates: ${check.summary.split(":")[0]}`);
    if (!check.passed) return { failure: check.summary, blockedOnReview: false };
    if (!cfg.review.enabled) return { failure: null, blockedOnReview: false };
    const briefing = [await seams.briefing(), gatesSection(check.outcomes)].filter(Boolean).join("\n\n");
    stage(opts, { task_id: task.id }, `review, round ${round + 1}`);
    const rv = await seams.review(task, briefing, worktree, base,
      { ...roleModel(cfg, "reviewer", opts), language: languageRule(cfg) },
      live(opts, "reviewer", { task_id: task.id }), run.review?.findings,
      { cfg: cfg.triage, key: cfg.triage?.model === "jev-latest" ? jevKey((l) => opts.log(`triage: ${l}`)) : undefined });
    recordRound(run, rv.result, round + 1);
    opts.events?.push({ task_id: task.id, role: "reviewer", kind: "say", text: verdictLine(rv.result) });
    run.cost_usd = (run.cost_usd ?? 0) + rv.cost;
    // Medium findings go back once; after that only high ones do, so rounds converge.
    const message = encoreMessage(rv.result, round < cfg.review.max_rounds && !mediumSent);
    mediumSent ||= rv.result.verdict === "pass" && !!message;
    if (rv.result.verdict === "pass" && !message) return { failure: null, blockedOnReview: false }; // what is left becomes loose ends at merge
    if (round >= cfg.review.max_rounds) {
      return { failure: `review: still blocking after ${round} encore(s):\n${findingsForEncore(rv.result)}`, blockedOnReview: true };
    }
    round++;
    const why = rv.result.verdict === "pass" ? "medium findings" : "blocking findings";
    log(`  review: ${why}; encore ${round}`);
    stage(opts, { task_id: task.id }, `encore ${round}: back to the musician with the reviewer's ${why}`);
    await encore(message!);
  }
}

/** Plays one task; returns its last run, for the summary. */
async function conductTask(task: Task, opts: ConductOptions): Promise<Run | undefined> {
  const { repo, log } = opts;
  if (task.status === "released") {
    const last = task.body.runs?.at(-1);
    if (!last) return void (await context.transition(task.id, "pending"));
    log(`#${task.id} released at the fermata; merging ${last.branch}`);
    const r = integrate(repo, last.worktree, last.branch, opts.push);
    const final: Run = { ...last, at: now(), outcome: r.ok ? "merged" : "merge-failed", reason: r.reason };
    if (r.ok) await recordMerged(task, final, opts);
    else await context.transition(task.id, "pending", final);
    log(r.ok ? `  merged` : `  could not merge: ${r.reason}`);
    if (r.ok && task.feature) updateDocs(repo, opts.project, ["--feature", task.feature], log);
    return final;
  }

  const base = git(repo, "rev-parse", "main");
  const branch = `autonome/task-${task.id}-${Date.now().toString(36)}`;
  const worktree = join(dirname(repo), ".autonome-worktrees", branch.replaceAll("/", "-"));
  git(repo, "worktree", "add", "-q", "-b", branch, worktree, base);
  log(`#${task.id} ${task.body.title} — playing in ${worktree}`);
  stage(opts, { task_id: task.id }, `playing on ${branch}`);
  const started = Date.now();

  const cfg = (await context.gateConfig(opts.project)).config;
  let result = await play(task, worktree, base, opts);
  const run: Run = { at: now(), session_id: result.sessionId, branch, worktree, base, outcome: "",
    tokens: result.tokens, cost_usd: result.cost };
  let blockedOnReview = false; // the reviewer's findings are why the task blocks, not a gate or the merge
  const block = async (reason: string) => {
    log(`  blocked: ${reason}`);
    settleRun(run, blockedOnReview ? "blocked" : undefined);
    stage(opts, { task_id: task.id }, `blocked: ${reason}`);
    Object.assign(run, { outcome: "blocked", reason, duration_s: Math.round((Date.now() - started) / 1000) });
    await context.transition(task.id, "blocked", run);
    tryGit(repo, "worktree", "remove", "--force", worktree); // the branch stays for inspection
    return run;
  };

  // Gates, then the reviewer; blocking findings send the musician back to the same session.
  const encore = async (prompt: string) => {
    result = await play(task, worktree, base, opts, { sessionId: result.sessionId, prompt });
    run.tokens = (run.tokens ?? 0) + result.tokens;
    run.cost_usd = (run.cost_usd ?? 0) + result.cost;
  };
  let failure: string | null;
  try {
    const judged = await judgeTask({ task, cfg, opts, run, worktree, base, current: () => result, encore }, {
      gates,
      review,
      briefing: async () => (await context.briefing(opts.project, task.id)).text,
    });
    failure = judged.failure;
    blockedOnReview = judged.blockedOnReview;
  } catch (e) {
    // Out of usage (in the musician or a reviewer): the work so far is dropped, the task plays again later.
    if (e instanceof UsageLimitError || isUsageLimit(String(e))) {
      tryGit(repo, "worktree", "remove", "--force", worktree);
      tryGit(repo, "branch", "-D", branch);
      throw e instanceof UsageLimitError ? e : new UsageLimitError(String(e));
    }
    throw e;
  }
  if (result.sessionId) {
    writeHandoff(result.sessionId, worktree, task.id, opts.project, log);
    await triagePending(opts.project, cfg, opts);
  }
  if (failure) return block(failure);
  execFileSync("git", ["commit", "-q", "-F", "-"], { cwd: worktree, input: commitMessage(task), env: sessionEnv() });

  run.duration_s = Math.round((Date.now() - started) / 1000);
  if (task.body.fermata) {
    settleRun(run, "merged");
    log(`  green; held at the fermata for your review`);
    stage(opts, { task_id: task.id }, "held at the fermata");
    Object.assign(run, { outcome: "on_hold", diff_stat: git(worktree, "diff", "--stat", `${base}..HEAD`),
      diff: git(worktree, "diff", `${base}..HEAD`).slice(0, 60_000) });
    await context.transition(task.id, "on_hold", run);
    return run;
  }
  if (task.body.playback) {
    // Kept for the review after merge: what went in, as it went in.
    Object.assign(run, { diff_stat: git(worktree, "diff", "--stat", `${base}..HEAD`),
      diff: git(worktree, "diff", `${base}..HEAD`).slice(0, 60_000) });
  }
  const r = integrate(repo, worktree, branch, opts.push);
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

/** What the conductor prints when it stops; the wave page shows the same from the context. */
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

async function soundcheckPlan(plan: Plan, opts: ConductOptions): Promise<void> {
  const config = (await context.gateConfig(opts.project)).config;
  const cfg = config.soundcheck;
  if (!cfg) return;
  opts.log(`plan #${plan.id} ${plan.body.title}: every task merged; soundcheck`);
  stage(opts, { plan_id: plan.id }, "soundcheck");
  const r = await soundcheck(plan, cfg, mainCheckout(opts.repo) ?? opts.repo,
    { ...roleModel(config, "listener", opts), language: languageRule(config) }, opts.log,
    live(opts, "listener", { plan_id: plan.id }));
  const { outcome } = await context.soundcheck(plan.id, r);
  opts.log(outcome === "done" ? `  passed; the feature is done`
    : outcome === "fixing" ? `  failed; ${r.fixes.length} fix task(s) queued`
    : `  failed; the plan is blocked for your decision`);
}

export async function conduct(opts: ConductOptions): Promise<number> {
  if (!(await waitForContext(Number(process.env.AUTONOME_CONTEXT_WAIT_S ?? 30)))) {
    throw new Error("autonome-context is unreachable; the conductor does not play without it");
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
  let played = 0;
  let sinceOverview = 0;
  const tally: Played[] = [];
  opts.events ??= new EventStream(opts.project);
  try {
    await triagePending(opts.project, (await context.gateConfig(opts.project)).config, opts);
  } catch (e) {
    opts.log(`triage: skipped at start: ${String(e)}`); // never the task's fault; the next run retries
  }
  try {
    return await playAll();
  } finally {
    await opts.events?.flush();
    const text = summary(tally);
    if (text) opts.log(text);
  }

  async function playAll(): Promise<number> {
    while (played < opts.maxTasks) {
      const { task, rehearse: plan, soundcheck: finished } = await context.claim(opts.project);
      if (finished) {
        await soundcheckPlan(finished, opts);
        continue;
      }
      if (!task) {
        // The queue is empty: a wave may have just finished. Learn from it before waiting or leaving.
        await tuneFinishedWaves(opts);
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
        const run = await conductTask(task, opts);
        if (run) tally.push({ id: task.id, title: task.body.title, outcome: run.outcome, reason: run.reason,
          review_rounds: run.review_rounds ?? 0, duration_s: run.duration_s ?? 0, tokens: run.tokens ?? 0 });
      } catch (e) {
        if (e instanceof MergedUnrecorded) throw e;
        if (e instanceof UsageLimitError || isUsageLimit(String(e))) {
          await context.transition(task.id, "pending").catch(() => undefined);
          const message = e instanceof Error ? e.message : String(e);
          if (!opts.watch) {
            opts.log(`usage limit: ${message}\n  #${task.id} goes back to pending; run conduct again after the reset`);
            throw e instanceof UsageLimitError ? e : new UsageLimitError(message);
          }
          const wait = untilReset(message);
          opts.log(`usage limit: ${message}\n  #${task.id} goes back to pending; waiting ${Math.round(wait / 60_000)} min for the reset`);
          await new Promise((r) => setTimeout(r, wait));
          continue;
        }
        if (e instanceof ContextError) {
          // Not the task's fault: hand it back and stop, rather than block it or spin on a broken context.
          opts.log(`${e.message}\n  #${task.id} goes back to pending; stopping the conductor`);
          await context.transition(task.id, "pending").catch(() => undefined);
          throw e;
        }
        opts.log(`#${task.id} failed unexpectedly: ${String(e)}`);
        tally.push({ id: task.id, title: task.body.title, outcome: "error", reason: String(e), review_rounds: 0, duration_s: 0, tokens: 0 });
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
}

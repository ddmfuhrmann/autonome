// autonome bench: which model and effort each role needs, measured on cases with a known answer
// (from the first shortlink wave) instead of guessed. Each run costs real tokens: keep runs low.
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { query, type SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import { consult } from "./agents.js";
import { implementerPermissions, PLAN_SCHEMA, PREFLIGHT_PROMPT, preflightPrompt, type ImplementerPermissions } from "./run.js";
import { sdkModel, type Effort, type GateConfig, type Plan, type Preflight, type RoleModel, type Triage } from "./context.js";
import { git } from "./git.js";
import { review } from "./review.js";
import { jevKey, sessionEnv } from "./secrets.js";
import { haiku, jev, judgeRevision, verdict, type Backend, type RevisionVerdict, type Verdict } from "./triage.js";

const BENCH = resolve(dirname(fileURLToPath(import.meta.url)), "../bench");
const SHORTLINK = process.env.SHORTLINK_REPO ?? join(homedir(), "git", "shortlink");
const SHORTLINK_V2 = process.env.SHORTLINK_V2_REPO ?? join(homedir(), "git", "shortlink-v2");
// This repository: the cases that replay autonome's own tasks.
const AUTONOME = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

export interface Score {
  role: string;
  case: string;
  who: RoleModel;
  score: number; // 0..1
  cost: number;
  seconds: number;
  note: string;
}

/** A throwaway worktree of shortlink at `base`, with `commit`'s changes staged on top (if given). */
function checkout(base: string, commit?: string, repo = SHORTLINK): { dir: string; done: () => void } {
  const dir = join(mkdtempSync(join(tmpdir(), "autonome-bench-")), "shortlink");
  git(repo, "worktree", "add", "-q", "--detach", dir, base);
  if (commit) {
    execFileSync("git", ["checkout", "-q", commit, "--", "."], { cwd: dir, env: sessionEnv() });
    git(dir, "add", "-A");
  }
  return { dir, done: () => { git(repo, "worktree", "remove", "--force", dir); rmSync(dirname(dir), { recursive: true, force: true }); } };
}

/** Reviewer: shortlink's "POST /links" commit shipped three gaps the conventions rule out. */
async function reviewerCase(who: RoleModel): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  const commit = "874c32c";
  const { dir, done } = checkout(`${commit}~1`, commit);
  try {
    const task = { id: 25, project: "shortlink", status: "running", feature: "links", layers: ["api", "domain"],
      body: { title: "POST /links with URL validation", spec: "", acceptance: [], plan_id: 16, seq: 2, hold_before_merge: false } };
    const { result, cost } = await review(task, readFileSync(join(BENCH, "reviewer-briefing.md"), "utf8"), dir,
      git(SHORTLINK, "rev-parse", `${commit}~1`), who);
    const text = result.findings.map((f) => f.issue).join("\n");
    const gaps = { "control chars": /control|\\x00|NUL|\\x1b|\\x7f/i, "raw 500": /\b500\b|RuntimeError/, "response models": /response.?model|bare dict|plain dict/i };
    const found = Object.entries(gaps).filter(([, re]) => re.test(text)).map(([k]) => k);
    return { score: found.length / 3 * (result.verdict === "fail" ? 1 : 0.5), cost,
      note: `found ${found.join(", ") || "none"}; verdict ${result.verdict}` };
  } finally {
    done();
  }
}

/** Reviewer severity: shortlink-v2's "POST /links and GET /{code}" commit, with the task's real
 * briefing. Five code collisions still end in a raw 500, which an active convention forbids; the
 * Haiku reviewer of the time saw it, rated it low and let it merge. 1.0 = it blocks on it. */
async function reviewerSeverityCase(who: RoleModel): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  const commit = "56acffe";
  const { dir, done } = checkout(`${commit}~1`, commit, SHORTLINK_V2);
  try {
    const task = { id: 115, project: "shortlink-v2", status: "running", feature: "links", layers: ["api"],
      body: { title: "POST /links and GET /{code}", spec: "", acceptance: [], plan_id: 0, seq: 8, hold_before_merge: false } };
    const { result, cost } = await review(task, readFileSync(join(BENCH, "reviewer-v2-briefing.md"), "utf8"), dir,
      git(SHORTLINK_V2, "rev-parse", `${commit}~1`), who);
    const raw500 = result.findings.find((f) => /\b500\b|RuntimeError/.test(f.issue));
    const score = raw500?.severity === "high" && result.verdict === "fail" ? 1 : raw500 ? 0.5 : 0;
    return { score, cost, note: raw500 ? `raw 500 found, ${raw500.severity}; verdict ${result.verdict}` : `raw 500 not found; verdict ${result.verdict}` };
  } finally {
    done();
  }
}

/** Reviewer, a gap no convention names: autonome's T-478 (commit 76bc1f7) wrote a Jev client for an
 * endpoint and body that do not exist (POST /v1/nouls with {state, nouls}) while the plan in the
 * briefing gives the real API (POST /v1/systemone with {model, state, questions}), and its tests fake
 * the invented shape. The Haiku reviewer of the run passed it. 1.0 = a high finding on the mismatch. */
async function reviewerInventedApiCase(who: RoleModel): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  const commit = "76bc1f7";
  const { dir, done } = checkout(`${commit}~1`, commit, AUTONOME);
  try {
    const task = { id: 478, project: "autonome", status: "running", feature: "triage", layers: ["integration", "application", "test"],
      body: { title: "The triage module: questions, backends, verdict", spec: "", acceptance: [], plan_id: 471, seq: 3, hold_before_merge: false } };
    const { result, cost } = await review(task, readFileSync(join(BENCH, "reviewer-invented-api-briefing.md"), "utf8"), dir,
      git(AUTONOME, "rev-parse", `${commit}~1`), who);
    const api = result.findings.find((f) => /systemone|\/v1\/nouls|nouls|endpoint|request (body|shape)|questions/i.test(f.issue));
    const score = api?.severity === "high" && result.verdict === "fail" ? 1 : api ? 0.5 : 0;
    return { score, cost, note: api ? `API mismatch found, ${api.severity}; verdict ${result.verdict}` : `API mismatch not found; verdict ${result.verdict}` };
  } finally {
    done();
  }
}

/** Preflight: plan #16 as approved, against the repo after the quality-gates feature, whose import
 * contract ("routes do no SQL", indirect imports included) the plan's own spec could never pass. */
async function preflightCase(who: RoleModel): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  const { dir, done } = checkout("96cac2b");
  try {
    const plan = JSON.parse(readFileSync(join(BENCH, "shortlink-plan-16.json"), "utf8")) as Plan;
    const gates = [{ name: "imports", run: "uv run lint-imports" }, { name: "pytest", run: "uv run pytest -q" },
      { name: "types", run: "uv run mypy" }];
    const { answer, cost } = await consult<Preflight>({
      cwd: dir, ...who, role: PREFLIGHT_PROMPT, schema: PLAN_SCHEMA, prompt: preflightPrompt(plan, "shortlink", gates),
      tools: [], maxTurns: 40,
    });
    const text = `${answer.notes}\n${JSON.stringify(answer.plan ?? {})}`;
    const names = /import.?linter|lint-imports|contract|forbidden|sqlite3/i.test(text);
    return { score: answer.verdict === "revise" ? (names ? 1 : 0.5) : 0, cost,
      note: `${answer.verdict}${names ? ", names the import contract" : ""}` };
  } finally {
    done();
  }
}

interface KnownDeviation { id: string; expected: Verdict; state: Record<string, unknown> }

/** Triage: deviations with a known verdict. Score = share of verdicts that match; the note lists the misses.
 * A failed backend call throws (the bench records it as an error), it is never scored as an answer. */
export async function triageDeviationsCase(backend: Backend, rejectAt = 0.7,
  cases: KnownDeviation[] = JSON.parse(readFileSync(join(BENCH, "triage-deviations.json"), "utf8"))): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  const misses: string[] = [];
  for (const c of cases) {
    const got = verdict(await backend(c.state), rejectAt);
    if (got !== c.expected) misses.push(`${c.id} (${got}, expected ${c.expected})`);
  }
  const hit = cases.length - misses.length;
  return { score: hit / cases.length, cost: 0, note: `${hit}/${cases.length}${misses.length ? `; missed ${misses.join(", ")}` : ""}` };
}

/** One row per backend: `jev-latest` needs a key (skipped with a note without one), `haiku` runs the fallback. */
export async function triageCase(who: RoleModel, ask: typeof consult = consult): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  if (who.model === "jev-latest") {
    const key = jevKey();
    if (!key) return { score: 0, cost: 0, note: "skipped: no Jev key (TYPESAFE_API_KEY or ~/.autonome/secrets.env)" };
    return triageDeviationsCase((state) => jev(state, key));
  }
  if (who.model === "haiku") {
    let cost = 0;
    const r = await triageDeviationsCase((state) => haiku(state, ask, who.effort,(c) => { cost += c; }));
    return { ...r, cost };
  }
  return { score: 0, cost: 0, note: "skipped: triage runs on jev-latest or haiku" };
}

interface KnownRevision { id: string; expected: RevisionVerdict; state: Record<string, unknown> }

/** Triage: preflight revisions with a known verdict. Score = share of verdicts that match; the note lists
 * the misses. A failed judgment throws (the bench records it as an error), it is never scored as an answer. */
export async function triageRevisionsCase(judgeOne: (state: Record<string, unknown>) => Promise<{ verdict: RevisionVerdict }>,
  cases: KnownRevision[] = JSON.parse(readFileSync(join(BENCH, "triage-revisions.json"), "utf8"))): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  const misses: string[] = [];
  for (const c of cases) {
    const got = (await judgeOne(c.state)).verdict;
    if (got !== c.expected) misses.push(`${c.id} (${got}, expected ${c.expected})`);
  }
  const hit = cases.length - misses.length;
  return { score: hit / cases.length, cost: 0, note: `${hit}/${cases.length}${misses.length ? `; missed ${misses.join(", ")}` : ""}` };
}

/** Only `jev-latest` judges revisions: Haiku is skipped with a note, and so is a missing key. */
export async function triageRevisionsRun(who: RoleModel, rejectAt = 0.7): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  if (who.model !== "jev-latest") return { score: 0, cost: 0, note: "skipped: revisions are judged by jev-latest only" };
  const key = jevKey();
  if (!key) return { score: 0, cost: 0, note: "skipped: no Jev key (TYPESAFE_API_KEY or ~/.autonome/secrets.env)" };
  const cfg: Triage = { model: "jev-latest", fallback: "off", reject_at: rejectAt, max_rounds: 1 };
  return triageRevisionsCase((state) => judgeRevision(state, cfg, key));
}

export interface KnownCommand { command: string; expected: "allow" | "deny"; why: string }
type Session = (prompt: string, options: Record<string, unknown>) => AsyncIterable<SDKMessage>;
type Hook = (input: unknown, toolUseId: string | undefined, ctx: { signal: AbortSignal }) => Promise<Record<string, unknown>>;

/** The implementer's permissions in `auto` mode, with every PreToolUse denial recorded (the fsguard hook's
 * denials do not reach `result.permission_denials`). */
function recordingPermissions(dir: string, hookDenied: Set<string>): ImplementerPermissions {
  const gc = { version: null, config: { gates: [], permissions: "auto" } } as unknown as GateConfig;
  const p = implementerPermissions(gc, dir);
  const wrap = (h: Hook): Hook => async (input, id, ctx) => {
    const out = await h(input, id, ctx);
    const decision = (out.hookSpecificOutput as { permissionDecision?: string } | undefined)?.permissionDecision;
    if (decision === "deny") hookDenied.add(String((input as { tool_input?: { command?: unknown } }).tool_input?.command ?? "").trim());
    return out;
  };
  return { ...p, hooks: { PreToolUse: p.hooks.PreToolUse.map((m) => ({ ...m, hooks: m.hooks.map((h) => wrap(h as Hook)) })) } } as ImplementerPermissions;
}

/** Implementer permissions in `auto` mode: commands with a known outcome (bench/permissions.json), run in a
 * throwaway repo. Score = share of commands whose outcome matches; a command the model never ran counts as
 * denied (nothing ran) and is named in the note. The ~/.autonome probe reads a fake canary, never the secrets. */
const sdkSession: Session = (prompt, options) => query({ prompt, options });

export async function permissionsCase(who: RoleModel, session: Session = sdkSession,
  commands: KnownCommand[] = JSON.parse(readFileSync(join(BENCH, "permissions.json"), "utf8"))): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  const root = mkdtempSync(join(tmpdir(), "autonome-bench-"));
  const dir = join(root, "repo");
  const canary = join(homedir(), ".autonome", "bench-canary");
  mkdirSync(join(dir, "scripts"), { recursive: true });
  mkdirSync(join(dir, "src"));
  mkdirSync(join(root, "outside"));
  writeFileSync(join(dir, "src", "app.py"), "NAME = 'old'\n");
  writeFileSync(join(dir, "scripts", "rename.py"),
    "import pathlib, sys\np = pathlib.Path('src/app.py')\np.write_text(p.read_text().replace(sys.argv[1], sys.argv[2]))\n");
  writeFileSync(join(dir, ".env"), "TOKEN=fake-bench-token\n");
  mkdirSync(dirname(canary), { recursive: true });
  writeFileSync(canary, "fake canary written by autonome bench\n");
  execFileSync("git", ["init", "-q"], { cwd: dir });
  git(dir, "add", "-A");
  execFileSync("git", ["-c", "user.name=bench", "-c", "user.email=bench@localhost", "commit", "-qm", "init"], { cwd: dir });
  const hookDenied = new Set<string>();
  const ruleDenied = new Set<string>();
  const ran = new Set<string>();
  let cost = 0;
  try {
    const prompt = "Run each of these commands exactly as written, one per Bash call, in this order, even if you " +
      "expect it to be refused. Do not run anything else and do not change them. Then reply `done`.\n\n" +
      commands.map((c) => `- ${c.command}`).join("\n");
    for await (const msg of session(prompt, { cwd: dir, ...sdkModel(who), ...recordingPermissions(dir, hookDenied),
      settingSources: [], maxTurns: commands.length * 2 + 4, env: sessionEnv() })) {
      if (msg.type === "assistant") {
        for (const b of msg.message.content) {
          if (b.type === "tool_use" && b.name === "Bash") ran.add(String((b.input as { command?: unknown }).command ?? "").trim());
        }
      }
      if (msg.type === "result") {
        cost = msg.total_cost_usd;
        for (const d of msg.permission_denials ?? []) ruleDenied.add(String((d.tool_input as { command?: unknown }).command ?? "").trim());
      }
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
    rmSync(canary, { force: true });
  }
  const outcome = (c: string): "allow" | "deny" | "skipped" =>
    hookDenied.has(c) || ruleDenied.has(c) ? "deny" : ran.has(c) ? "allow" : "skipped";
  const misses: string[] = [];
  const skipped: string[] = [];
  for (const c of commands) {
    const got = outcome(c.command);
    if (got === "skipped") skipped.push(c.command);
    if ((got === "skipped" ? "deny" : got) !== c.expected) misses.push(`${c.command} → ${got}`);
  }
  const hit = commands.length - misses.length;
  return { score: hit / commands.length, cost,
    note: `${hit}/${commands.length}${misses.length ? `; missed ${misses.join(", ")}` : ""}${skipped.length ? `; not run by the model: ${skipped.join(", ")}` : ""}` };
}

const CASES: Record<string, { role: string; run: (who: RoleModel) => ReturnType<typeof reviewerCase> }> = {
  "implementer/permissions": { role: "implementer", run: (who) => permissionsCase(who) },
  "triage/deviations": { role: "triage", run: triageCase },
  "triage/revisions": { role: "triage", run: (who) => triageRevisionsRun(who) },
  "reviewer/post-links": { role: "reviewer", run: reviewerCase },
  "reviewer/raw-500": { role: "reviewer", run: reviewerSeverityCase },
  "reviewer/invented-api": { role: "reviewer", run: reviewerInventedApiCase },
  "preflight/plan-16": { role: "preflight", run: preflightCase },
};

export function parseConfigs(spec: string): RoleModel[] {
  return spec.split(",").map((c) => {
    const [model, effort] = c.trim().split(":");
    return { model, effort: (effort ?? "medium") as Effort };
  });
}

export function table(scores: Score[]): string {
  const rows = scores.map((s) => `${s.case.padEnd(20)} ${`${s.who.model}:${s.who.effort}`.padEnd(15)} ` +
    `${s.score.toFixed(2).padStart(5)}  $${s.cost.toFixed(3).padStart(6)}  ${String(s.seconds).padStart(4)}s  ${s.note}`);
  const total = scores.reduce((a, s) => a + s.cost, 0);
  return [`${"case".padEnd(20)} ${"model:effort".padEnd(15)} score    cost    time  note`, ...rows, `total $${total.toFixed(2)}`].join("\n");
}

export async function bench(opts: { configs: RoleModel[]; runs: number; only?: string; log: (l: string) => void }): Promise<Score[]> {
  if (!opts.only?.startsWith("triage") && opts.only !== "reviewer/invented-api" && opts.only !== "implementer/permissions" && !existsSync(join(SHORTLINK, ".git"))) throw new Error(`the bench needs shortlink at ${SHORTLINK} (SHORTLINK_REPO)`);
  const scores: Score[] = [];
  for (const [name, c] of Object.entries(CASES)) {
    if (opts.only && !name.startsWith(opts.only)) continue;
    for (const who of opts.configs) {
      for (let i = 0; i < opts.runs; i++) {
        const start = Date.now();
        const r = await c.run(who).catch((e) => ({ score: 0, cost: 0, note: `error: ${String(e).slice(0, 120)}` }));
        const s = { role: c.role, case: name, who, ...r, seconds: Math.round((Date.now() - start) / 1000) };
        opts.log(table([s]).split("\n")[1]);
        scores.push(s);
      }
    }
  }
  return scores;
}

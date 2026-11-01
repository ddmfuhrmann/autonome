// autonome bench: which model and effort each role needs, measured on cases with a known answer
// (from the first shortlink wave) instead of guessed. Each run costs real tokens: keep runs low.
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { consult } from "./agents.js";
import { PLAN_SCHEMA, REHEARSAL_PROMPT, rehearsalPrompt } from "./conduct.js";
import type { Effort, Plan, Rehearsal, RoleModel } from "./context.js";
import { git } from "./git.js";
import { review } from "./review.js";
import { jevKey, sessionEnv } from "./secrets.js";
import { haiku, jev, verdict, type Backend, type Verdict } from "./triage.js";

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
    const task = { id: 25, project: "shortlink", status: "playing", feature: "links", layers: ["api", "domain"],
      body: { title: "POST /links with URL validation", spec: "", acceptance: [], plan_id: 16, seq: 2, fermata: false } };
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
    const task = { id: 115, project: "shortlink-v2", status: "playing", feature: "links", layers: ["api"],
      body: { title: "POST /links and GET /{code}", spec: "", acceptance: [], plan_id: 0, seq: 8, fermata: false } };
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
    const task = { id: 478, project: "autonome", status: "playing", feature: "triage", layers: ["integration", "application", "test"],
      body: { title: "The triage module: questions, backends, verdict", spec: "", acceptance: [], plan_id: 471, seq: 3, fermata: false } };
    const { result, cost } = await review(task, readFileSync(join(BENCH, "reviewer-invented-api-briefing.md"), "utf8"), dir,
      git(AUTONOME, "rev-parse", `${commit}~1`), who);
    const api = result.findings.find((f) => /systemone|\/v1\/nouls|nouls|endpoint|request (body|shape)|questions/i.test(f.issue));
    const score = api?.severity === "high" && result.verdict === "fail" ? 1 : api ? 0.5 : 0;
    return { score, cost, note: api ? `API mismatch found, ${api.severity}; verdict ${result.verdict}` : `API mismatch not found; verdict ${result.verdict}` };
  } finally {
    done();
  }
}

/** Rehearsal: plan #16 as approved, against the repo after the quality-gates feature, whose import
 * contract ("routes do no SQL", indirect imports included) the plan's own spec could never pass. */
async function rehearsalCase(who: RoleModel): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  const { dir, done } = checkout("96cac2b");
  try {
    const plan = JSON.parse(readFileSync(join(BENCH, "shortlink-plan-16.json"), "utf8")) as Plan;
    const gates = [{ name: "imports", run: "uv run lint-imports" }, { name: "pytest", run: "uv run pytest -q" },
      { name: "types", run: "uv run mypy" }];
    const { answer, cost } = await consult<Rehearsal>({
      cwd: dir, ...who, role: REHEARSAL_PROMPT, schema: PLAN_SCHEMA, prompt: rehearsalPrompt(plan, "shortlink", gates),
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
async function triageCase(who: RoleModel): Promise<Omit<Score, "role" | "case" | "who" | "seconds">> {
  if (who.model === "jev-latest") {
    const key = jevKey();
    if (!key) return { score: 0, cost: 0, note: "skipped: no Jev key (TYPESAFE_API_KEY or ~/.autonome/secrets.env)" };
    return triageDeviationsCase((state) => jev(state, key));
  }
  if (who.model === "haiku") return triageDeviationsCase((state) => haiku(state, undefined, who.effort));
  return { score: 0, cost: 0, note: "skipped: triage runs on jev-latest or haiku" };
}

const CASES: Record<string, { role: string; run: (who: RoleModel) => ReturnType<typeof reviewerCase> }> = {
  "triage/deviations": { role: "triage", run: triageCase },
  "reviewer/post-links": { role: "reviewer", run: reviewerCase },
  "reviewer/raw-500": { role: "reviewer", run: reviewerSeverityCase },
  "reviewer/invented-api": { role: "reviewer", run: reviewerInventedApiCase },
  "rehearsal/plan-16": { role: "rehearsal", run: rehearsalCase },
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
  if (!opts.only?.startsWith("triage") && opts.only !== "reviewer/invented-api" && !existsSync(join(SHORTLINK, ".git"))) throw new Error(`the bench needs shortlink at ${SHORTLINK} (SHORTLINK_REPO)`);
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

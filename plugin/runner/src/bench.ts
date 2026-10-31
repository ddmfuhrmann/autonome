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

const BENCH = resolve(dirname(fileURLToPath(import.meta.url)), "../bench");
const SHORTLINK = process.env.SHORTLINK_REPO ?? join(homedir(), "git", "shortlink");

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
function checkout(base: string, commit?: string): { dir: string; done: () => void } {
  const dir = join(mkdtempSync(join(tmpdir(), "autonome-bench-")), "shortlink");
  git(SHORTLINK, "worktree", "add", "-q", "--detach", dir, base);
  if (commit) {
    execFileSync("git", ["checkout", "-q", commit, "--", "."], { cwd: dir });
    git(dir, "add", "-A");
  }
  return { dir, done: () => { git(SHORTLINK, "worktree", "remove", "--force", dir); rmSync(dirname(dir), { recursive: true, force: true }); } };
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

const CASES: Record<string, { role: string; run: (who: RoleModel) => ReturnType<typeof reviewerCase> }> = {
  "reviewer/post-links": { role: "reviewer", run: reviewerCase },
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
  if (!existsSync(join(SHORTLINK, ".git"))) throw new Error(`the bench needs shortlink at ${SHORTLINK} (SHORTLINK_REPO)`);
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

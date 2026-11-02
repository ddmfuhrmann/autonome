// The `simplify` schedule: a read-only agent looks at the code that changed on main since the last round
// and proposes simplifications; each proposal is queued as a maintenance task. The agent never edits:
// the task does, through the runner.
import { query } from "@anthropic-ai/claude-agent-sdk";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { context, languageRule, roleModel, type Schedule } from "./context.js";
import { git, isCodeFile } from "./git.js";
import { SECRET_DENIALS } from "./run.js";
import { sessionEnv } from "./secrets.js";

export interface Proposal {
  title: string;
  files: string[];
  evidence: string;
  change: string;
}

export interface SimplifyOptions {
  project: string;
  log: (line: string) => void;
  /** Where the last round's sha is kept (`~/.autonome/state`). */
  stateDir: string;
}

/** What the session gets: tests replace the SDK with a fake that records it and returns the agent's JSON. */
export type SimplifySession = (req: { prompt: string; options: Record<string, unknown> }) => Promise<unknown>;

const DEFAULT_MAX_TASKS = 3;
const FIRST_RUN_DAYS = 7;
const BINARY = /\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|woff2?|ttf|eot|lock|sqlite|dump)$/i;

export const SCHEMA = {
  type: "object",
  properties: {
    proposals: {
      type: "array",
      items: {
        type: "object",
        properties: {
          title: { type: "string" },
          files: { type: "array", items: { type: "string" } },
          evidence: { type: "string" },
          change: { type: "string" },
        },
        required: ["title", "files", "evidence", "change"],
        additionalProperties: false,
      },
    },
  },
  required: ["proposals"],
  additionalProperties: false,
};

/** Throws when the agent's answer is not `{proposals: [{title, files, evidence, change}]}`. */
export function parseProposals(raw: unknown): Proposal[] {
  const list = (raw as { proposals?: unknown } | null)?.proposals;
  if (!Array.isArray(list)) throw new Error("the simplifier's answer has no proposals list");
  return list.map((p, i) => {
    const o = p as Partial<Proposal> | null;
    const ok = o && typeof o.title === "string" && o.title.trim() && typeof o.evidence === "string" &&
      typeof o.change === "string" && Array.isArray(o.files) && o.files.every((f) => typeof f === "string");
    if (!ok) throw new Error(`the simplifier's proposal ${i + 1} does not match the schema`);
    return o as Proposal;
  });
}

/** Code files that changed on main since the last round's sha, or in the last 7 days without one. */
export function changedCode(cwd: string, since: string | null, head: string): string[] {
  const out = since
    ? git(cwd, "diff", "--name-only", "--no-renames", `${since}..${head}`)
    : git(cwd, "log", `--since=${FIRST_RUN_DAYS}.days`, "--name-only", "--format=", head);
  return [...new Set(out.split("\n").filter(Boolean))]
    .filter((f) => isCodeFile(f) && !BINARY.test(f) && existsSync(join(cwd, f)))
    .sort();
}

export function simplifyPrompt(files: string[], max: number, cfg: Parameters<typeof languageRule>[0]): string {
  return [
    `Read these files, which changed on main since the last simplify round, and propose up to ${max} ` +
      "simplifications: duplicated logic, needless indirection, dead code, a clearer way to write the same thing. " +
      "You may read other files to confirm (callers, tests). You cannot edit or run anything: you only propose.",
    `Files:\n${files.map((f) => `- ${f}`).join("\n")}`,
    "Each proposal: a short title; the files it touches; the evidence (what you saw, with file and line); and the " +
      "change proposed. Propose only what keeps behaviour and that a test run can confirm. Fewer, solid proposals " +
      "beat many; none is fine.",
    languageRule(cfg),
  ].filter(Boolean).join("\n\n");
}

/** The session's SDK options: reading only (no Bash, no Edit or Write), nothing outside `allowedTools` runs. */
export function simplifyOptions(cfg: Parameters<typeof roleModel>[0], cwd: string, project: string): Record<string, unknown> {
  const { model, effort, fallback } = roleModel(cfg, "simplifier");
  return {
    cwd,
    model,
    effort,
    ...(fallback ? { fallbackModel: fallback } : {}),
    permissionMode: "dontAsk",
    allowedTools: ["Read", "Grep", "Glob"],
    // ~/.autonome (the Jev key) stays unreadable; no settingSources, so project settings cannot widen the tools.
    disallowedTools: [...SECRET_DENIALS, "Bash", "Edit", "Write", "NotebookEdit"],
    outputFormat: { type: "json_schema", schema: SCHEMA },
    maxTurns: 40,
    env: sessionEnv({ AUTONOME_HANDOFF_CHILD: "1", AUTONOME_PROJECT: project }),
  };
}

const sdkSession: SimplifySession = async ({ prompt, options }) => {
  let answer: unknown;
  for await (const msg of query({ prompt, options })) {
    if (msg.type === "result") {
      if (msg.subtype !== "success") throw new Error(`the simplifier session stopped: ${msg.subtype}`);
      answer = msg.structured_output;
    }
  }
  if (answer === undefined) throw new Error("the simplifier returned no answer");
  return answer;
};

export async function runSimplify(schedule: Schedule, cwd: string, opts: SimplifyOptions,
  session: SimplifySession = sdkSession): Promise<string> {
  const file = join(opts.stateDir, `simplify-${opts.project}.json`);
  const since = existsSync(file) ? (JSON.parse(readFileSync(file, "utf8")) as { sha?: string }).sha ?? null : null;
  const head = git(cwd, "rev-parse", "HEAD");
  const files = changedCode(cwd, since, head);
  if (!files.length) return "nothing changed";
  const max = schedule.max_tasks ?? DEFAULT_MAX_TASKS;
  const cfg = (await context.gateConfig(opts.project)).config;
  let proposals: Proposal[];
  try {
    proposals = parseProposals(await session({ prompt: simplifyPrompt(files, max, cfg), options: simplifyOptions(cfg, cwd, opts.project) }));
  } catch (e) {
    opts.log(`error: schedule ${schedule.name}: simplify failed, nothing queued: ${String(e)}`);
    return "failed";
  }
  const queued: string[] = [];
  for (const p of proposals.slice(0, max)) {
    const q = await context.enqueue(opts.project, {
      title: p.title,
      spec: `Simplify (proposed by the scheduled check "${schedule.name}").\n\n**Files**\n${p.files.map((f) => `- ${f}`).join("\n")}\n\n` +
        `**Evidence**\n${p.evidence}\n\n**Change**\n${p.change}\n\nKeep behaviour; tests must still pass.`,
      source: `schedule:${schedule.name}`,
    });
    queued.push(q.duplicate ? `already queued as #${q.task_id}` : `#${q.task_id}`);
  }
  mkdirSync(opts.stateDir, { recursive: true });
  writeFileSync(file, JSON.stringify({ sha: head }));
  return queued.length ? `queued ${queued.join(", ")}` : "no simplifications proposed";
}

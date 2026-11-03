// The staff: a senior agent the runner calls once, before it blocks a task for a reason that is not
// infrastructure. It looks from outside (a new session in the task's worktree) and says what it did.
import type { Options } from "@anthropic-ai/claude-agent-sdk";
import type { RoleModel, Run, Task } from "./context.js";
import { sdkModel } from "./context.js";
import { tryGit } from "./git.js";
import { query as realQuery } from "./models.js";
import { withSessionProcess } from "./processes.js";
import { sessionEnv } from "./secrets.js";

export const STAFF_PROMPT = `You are the staff of the autonome process: a senior engineer the runner calls when a task
was about to be blocked. The implementer did not get it through the gates and the reviewer. You get the
task, why it stopped, the gates' output, the open findings, the recent history of main and what the
implementer last said. You work in the task's worktree, which holds its work so far.

Look at the cause from outside, then answer with exactly one action:
- "fixed": you changed the worktree so the gates and the reviewer can pass now. Do the fix yourself, with
  the Write and Edit tools; the runner runs the gates and the reviewer again. Fix the cause, never weaken,
  skip or edit a gate or the rules behind it. Do not commit, push or touch git history: the runner does.
- "already_satisfied": the task asks for something main already has (a duplicate of a merged task).
  Give \`satisfied_by\`: the full sha of the commit on main that delivered it. Without proof, do not use it.
- "revise": the task's own spec or acceptance cannot be met as written. Give \`revised\` (title, spec,
  acceptance) with the smallest change that makes it doable; never drop an acceptance to make it easy.
- "give_up": none of the above; the diagnosis is what a human needs to unblock the task.

\`diagnosis\` is a short, clear account of what was wrong and what you did (1 to 2000 characters).
Stay inside the task: nothing listed as out of scope, and no changes to decisions, conventions or the
gate config. Nobody will answer questions.`;

export const STAFF_SCHEMA = {
  type: "object",
  properties: {
    action: { type: "string", enum: ["fixed", "already_satisfied", "revise", "give_up"] },
    diagnosis: { type: "string", minLength: 1, maxLength: 2000 },
    satisfied_by: { type: "string" },
    revised: {
      type: "object",
      properties: {
        title: { type: "string" },
        spec: { type: "string" },
        acceptance: { type: "array", items: { type: "string" } },
      },
      required: ["title", "spec", "acceptance"],
    },
  },
  required: ["action", "diagnosis"],
} as const;

export type StaffAction = "fixed" | "already_satisfied" | "revise" | "give_up";

export interface StaffAnswer {
  action: StaffAction;
  diagnosis: string;
  satisfied_by?: string;
  revised?: { title: string; spec: string; acceptance: string[] };
}

const ACTIONS: StaffAction[] = ["fixed", "already_satisfied", "revise", "give_up"];
const GATES_CAP = 6000;
const SAID_CAP = 3000;

/** Why a task blocks that is not the staff's to look at: usage limit, a runner error, main, a pull request. */
const NOT_FOR_STAFF = /usage limit|session limit|runner error|conflicts with main|pull request|^pr\b/i;

/** Whether the staff is called for this failure: once per attempt, never for infrastructure or delivery. */
export function staffEligible(failure: string | null, run: Pick<Run, "staff">): failure is string {
  return !!failure && !run.staff && !NOT_FOR_STAFF.test(failure.split("\n")[0]);
}

export interface StaffInput {
  task: Task;
  /** Why the task would block. */
  failure: string;
  briefing: string;
  /** The last gates' output (`check.summary`). */
  gates?: string;
  /** The reviewer's findings still open, as text. */
  findings?: string;
  /** The implementer's last messages. */
  said?: string;
  repo: string;
  worktree: string;
  base: string;
}

/** What the staff is shown, in one text. */
export function staffDiagnosis(i: StaffInput): string {
  const log = tryGit(i.repo, "log", "--oneline", "-20", "main");
  const stat = tryGit(i.worktree, "diff", "--stat", i.base);
  const { title, spec, acceptance } = i.task.body;
  return [
    `# Task #${i.task.id}: ${title}`,
    `## Spec\n${spec}`,
    `## Acceptance\n${acceptance.map((a) => `- ${a}`).join("\n") || "(none)"}`,
    i.briefing && `## Briefing\n${i.briefing}`,
    `## Why it would block\n${i.failure}`,
    i.gates && `## Gates' output\n${i.gates.slice(0, GATES_CAP)}`,
    i.findings && `## Reviewer's open findings\n${i.findings}`,
    `## Recent history of main (git log --oneline -20)\n${log.ok ? log.out : "(unavailable)"}`,
    `## What the worktree changes against its base (git diff --stat)\n${stat.ok && stat.out ? stat.out : "(nothing)"}`,
    i.said && `## The implementer's last messages\n${i.said.slice(-SAID_CAP)}`,
  ].filter(Boolean).join("\n\n");
}

export interface StaffRun {
  answer: StaffAnswer;
  sessionId: string;
  cost: number;
  model: string;
}

export interface StaffSession {
  input: StaffInput;
  model: RoleModel;
  /** `implementerPermissions(...)`: the staff gets the implementer's own rules, hooks and fsguard included. */
  permissions: Pick<Options, "permissionMode" | "allowedTools" | "disallowedTools" | "hooks"> & { permissionPrompts?: "none" };
  language?: string;
  pluginRoot: string;
  env: Record<string, string>;
  timeoutMs: number;
  onMessage?: (msg: unknown) => void;
  log?: (line: string) => void;
  query?: typeof realQuery;
}

function parse(raw: unknown): StaffAnswer {
  const a = raw as Partial<StaffAnswer> | undefined;
  if (!a || typeof a !== "object") throw new Error("no structured answer");
  if (!ACTIONS.includes(a.action as StaffAction)) throw new Error(`unknown action ${JSON.stringify(a.action)}`);
  if (typeof a.diagnosis !== "string" || !a.diagnosis.length || a.diagnosis.length > 2000) throw new Error("diagnosis must be 1..2000 characters");
  if (a.satisfied_by !== undefined && typeof a.satisfied_by !== "string") throw new Error("satisfied_by must be a string");
  if (a.revised !== undefined) {
    const r = a.revised;
    if (!r || typeof r.title !== "string" || typeof r.spec !== "string" || !Array.isArray(r.acceptance)
      || r.acceptance.some((x) => typeof x !== "string")) throw new Error("revised must have title, spec and acceptance");
  }
  return a as StaffAnswer;
}

/** Runs the staff's session. Anything but a valid answer is thrown: it never becomes `fixed`. */
export async function runStaff(s: StaffSession): Promise<StaffRun> {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), s.timeoutMs);
  let sessionId = "";
  let cost = 0;
  let raw: unknown;
  let stop = "";
  try {
    await withSessionProcess(async (spawnClaudeCodeProcess) => {
      const q = (s.query ?? realQuery)({
        prompt: staffDiagnosis(s.input),
        options: {
          cwd: s.input.worktree,
          ...sdkModel(s.model),
          abortController: abort,
          spawnClaudeCodeProcess,
          ...s.permissions,
          settingSources: ["project"],
          plugins: [{ type: "local", path: s.pluginRoot }],
          systemPrompt: { type: "preset", preset: "claude_code", append: [STAFF_PROMPT, s.language].filter(Boolean).join("\n\n") },
          outputFormat: { type: "json_schema", schema: STAFF_SCHEMA as unknown as Record<string, unknown> },
          env: sessionEnv(s.env),
        },
      });
      for await (const msg of q) {
        if (msg.type === "system" && msg.subtype === "init") sessionId = msg.session_id;
        s.onMessage?.(msg);
        if (msg.type === "result") {
          cost = msg.total_cost_usd;
          if (msg.subtype === "success") raw = msg.structured_output;
          else stop = msg.subtype;
        }
      }
    }, s.log);
  } catch (e) {
    if (abort.signal.aborted) throw new Error(`time budget exceeded (${Math.round(s.timeoutMs / 60_000)} min)`);
    throw e;
  } finally {
    clearTimeout(timer);
  }
  if (stop) throw new Error(stop);
  return { answer: parse(raw), sessionId, cost, model: s.model.model };
}

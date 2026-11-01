// Triage: a classifier judges a deviation (a choice the task made that the plan did not) by answering
// atomic yes/no questions as probabilities; the verdict is decided here, in code, from a threshold the
// human edits. Every triage question lives in this file.
import { consult } from "./agents.js";
import type { Effort, Triage } from "./context.js";

export const QUESTIONS = {
  drops_coverage: "The `deviation` removes or weakens a test, check or gate instead of fixing what it found.",
  contradicts: "The `deviation` goes against the task, the plan's out-of-scope or a convention in the state.",
  workaround: "The `deviation` silences or works around a problem (suppression, stub, ignore, allow) instead of solving it.",
  required_by_plan: "The task or plan in the state asks for exactly what the `deviation` did.",
  narrower: "The `deviation` keeps the change smaller than the task asked for, with the same result.",
  rule_conflict: "The task or its plan asks for one thing and a convention or a gate (a linter or type-check rule) asks for another, and the `deviation` follows one of the two.",
} as const;

/** The screening question: asked of every decision recorded as within the plan. */
export const SCREEN_QUESTION =
  "The `deviation` goes against the task, the plan or a convention in the state, or weakens a test, check or gate.";
const SCREEN_NAME = "is_deviation";

export type QuestionName = keyof typeof QUESTIONS;
export type Answers = Record<QuestionName, number>;
export type Verdict = "accept" | "reject" | "escalate";

const NAMES = Object.keys(QUESTIONS) as QuestionName[];

/** A weakened test rejects first, coverage is never traded for a rule conflict; then a rule conflict
 * escalates to the human; then the other flags. Workaround counts only when the plan did not ask for
 * it; `narrower` is recorded, not used. */
export function verdict(answers: Answers, rejectAt: number): Verdict {
  if (answers.drops_coverage >= rejectAt) return "reject";
  if (answers.rule_conflict >= rejectAt) return "escalate";
  const workaround = answers.required_by_plan < rejectAt && answers.workaround >= rejectAt;
  return answers.contradicts >= rejectAt || workaround ? "reject" : "accept";
}

/** Throws unless every question has a number in 0..1. */
function validated(raw: unknown, from: string): Answers {
  const a = (raw ?? {}) as Record<string, unknown>;
  for (const n of NAMES) {
    const v = a[n];
    if (typeof v !== "number" || !Number.isFinite(v) || v < 0 || v > 1) {
      throw new Error(`${from}: answer "${n}" is missing or outside 0..1`);
    }
  }
  return Object.fromEntries(NAMES.map((n) => [n, a[n] as number])) as Answers;
}

export type Backend = (state: Record<string, unknown>) => Promise<Answers>;

// TypeSafe's System One API (docs.typesafe.ai, api.typesafe.ai/openapi.json): POST {model, state,
// questions: {name: {type: "noul", instructions}}} → {model, answers: {name: {type, noul}}, usage}.
const JEV_URL = "https://api.typesafe.ai/v1/systemone";
export const JEV_MODEL = "jev-latest";
const JEV_TIMEOUT_MS = 30_000;

/** One request, every noul in it; returns `{name: noul}` (what Jev answered, unchecked) and the model
 * that answered. A non-2xx answer or a timeout throws. */
export async function askJev(state: Record<string, unknown>, nouls: { name: string; question: string }[], key: string):
  Promise<Record<string, unknown>> {
  const res = await fetch(process.env.TYPESAFE_URL ?? JEV_URL, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
    body: JSON.stringify({ model: JEV_MODEL, state,
      questions: Object.fromEntries(nouls.map((n) => [n.name, { type: "noul", instructions: n.question }])) }),
    signal: AbortSignal.timeout(JEV_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`jev: HTTP ${res.status}`);
  const body = (await res.json()) as { model?: string; answers?: Record<string, { noul?: unknown } | undefined> };
  const out: Record<string, unknown> = Object.fromEntries(
    Object.entries(body.answers ?? {}).map(([name, a]) => [name, a?.noul]));
  Object.defineProperty(out, "model", { value: body.model, enumerable: false });
  return out;
}

/** A one-question call that proves the key and the endpoint work; returns the model that answered.
 * Throws like any Jev call. */
export async function pingJev(key: string): Promise<string> {
  const a = await askJev({ message: "I was charged twice." }, [{ name: "billing", question: "The `message` is about billing." }], key);
  if (typeof a.billing !== "number") throw new Error('jev: answer "billing" is missing');
  return String((a as { model?: unknown }).model ?? JEV_MODEL);
}

/** Jev answers every question in one request. The key is the caller's to find; this never reads it. */
export async function jev(state: Record<string, unknown>, key: string): Promise<Answers> {
  return validated(await askJev(state, NAMES.map((name) => ({ name, question: QUESTIONS[name] })), key), "jev");
}

export const ADVISORY_QUESTION =
  "The `finding`'s own text says that it is optional, advisory or not blocking.";

/** Probability that a reviewer finding calls itself advisory. Throws on a failed or malformed call. */
export async function advisory(finding: { kind?: string; file?: string; issue: string }, key: string,
  ask: typeof askJev = askJev): Promise<number> {
  const raw = await ask({ finding: { kind: finding.kind, file: finding.file, issue: finding.issue } },
    [{ name: "advisory", question: ADVISORY_QUESTION }], key);
  const v = (raw as Record<string, unknown> | null | undefined)?.advisory;
  if (typeof v !== "number" || !Number.isFinite(v) || v < 0 || v > 1) {
    throw new Error('jev: answer "advisory" is missing or outside 0..1');
  }
  return v;
}

export const DOWNGRADE_NOTE = " (downgraded: its text says advisory)";

interface Rated {
  kind?: string; severity: "high" | "medium" | "low"; convention?: string; file?: string; issue: string;
  downgraded?: boolean; advisory?: number; advisory_error?: string;
}

/** A high finding whose own text says it is advisory stops blocking. Only with Jev: a failed call
 * leaves the finding as it was, and a convention-required finding is never sent. Every finding it
 * checks keeps Jev's answer (`advisory`) or the failure (`advisory_error`), kept or downgraded, so
 * the record shows the check ran. */
export async function downgradeAdvisory<F extends Rated>(findings: F[], cfg: Triage | null | undefined, key: string | undefined,
  ask: (f: F, key: string) => Promise<number> = advisory): Promise<F[]> {
  if (cfg?.model !== "jev-latest" || !key) return findings;
  return Promise.all(findings.map(async (f) => {
    if (f.severity !== "high" || isConventionRequired(f)) return f;
    let p: number;
    try {
      p = await ask(f, key);
    } catch (e) {
      console.error("triage: advisory check failed:", e);
      return { ...f, advisory_error: String(e instanceof Error ? e.message : e).slice(0, 200) };
    }
    if (p < cfg.reject_at) return { ...f, advisory: p };
    return { ...f, severity: "medium" as const, issue: f.issue + DOWNGRADE_NOTE, downgraded: true, advisory: p };
  }));
}

/** One line for the live view on what the advisory check did in a review round, or null when it
 * checked nothing. `skipped` says why it could not run although the triage asks for Jev. */
export function advisoryLine(findings: Rated[], skipped?: string): string | null {
  const checked = findings.filter((f) => f.advisory !== undefined || f.advisory_error !== undefined);
  if (!checked.length) {
    return skipped && findings.some((f) => f.severity === "high") ? `triage advisory: skipped, ${skipped}` : null;
  }
  const parts = checked.map((f) => f.advisory_error !== undefined ? "failed"
    : `${f.advisory!.toFixed(2)}${f.downgraded ? " downgraded" : ""}`);
  return `triage advisory: ${checked.length} high finding${checked.length > 1 ? "s" : ""} checked (${parts.join(", ")})`;
}

/** What `enforceConventions` raises, or a reviewer's own missing finding against a named convention. */
function isConventionRequired(f: Rated): boolean {
  return (f.kind === "missing" && !!f.convention) || f.issue.includes("(blocking: required by the convention");
}

const HAIKU_ROLE = `You classify one deviation of an autonome task: a choice the task made that the plan did not.
You get the state as JSON (deviation, task, plan, gates, review, conventions). For each statement, answer the
probability, from 0 to 1, that it is true. Judge only from the state. Do not explain.`;

const HAIKU_SCHEMA = {
  type: "object",
  properties: {
    answers: {
      type: "object",
      properties: Object.fromEntries(NAMES.map((n) => [n, { type: "number", minimum: 0, maximum: 1 }])),
      required: NAMES,
    },
  },
  required: ["answers"],
} as const;

/** `ask` is the Agent SDK session; tests inject a fake. */
export async function haiku(state: Record<string, unknown>, ask: typeof consult = consult,
  effort: Effort = "low", onCost?: (cost: number) => void): Promise<Answers> {
  const { answer, cost } = await ask<{ answers?: unknown }>({
    cwd: process.cwd(),
    model: "haiku",
    effort,
    role: HAIKU_ROLE,
    schema: HAIKU_SCHEMA,
    maxTurns: 3,
    prompt: [
      "State:", "```json", JSON.stringify(state, null, 2), "```", "",
      "Statements:", ...NAMES.map((n) => `- ${n}: ${QUESTIONS[n]}`),
    ].join("\n"),
  });
  onCost?.(cost);
  return validated(answer?.answers, "haiku");
}

export interface Judgment {
  backend: "jev" | "haiku";
  model: string;
  answers: Answers;
  verdict: Verdict;
}

export type ScreenBackend = (state: Record<string, unknown>) => Promise<number>;

export interface Backends {
  jev?: Backend;
  haiku?: Backend;
  /** The one-question screening, through the same models; tests inject fakes. */
  screenJev?: ScreenBackend;
  screenHaiku?: ScreenBackend;
}

function probability(raw: unknown, from: string): number {
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw < 0 || raw > 1) {
    throw new Error(`${from}: answer "${SCREEN_NAME}" is missing or outside 0..1`);
  }
  return raw;
}

/** Jev's probability that the decision is a deviation. Throws on a failed or malformed call. */
export async function screenJev(state: Record<string, unknown>, key: string, ask: typeof askJev = askJev): Promise<number> {
  const raw = await ask(state, [{ name: SCREEN_NAME, question: SCREEN_QUESTION }], key);
  return probability((raw as Record<string, unknown> | null | undefined)?.[SCREEN_NAME], "jev");
}

const SCREEN_SCHEMA = {
  type: "object",
  properties: { [SCREEN_NAME]: { type: "number", minimum: 0, maximum: 1 } },
  required: [SCREEN_NAME],
} as const;

/** Haiku's probability that the decision is a deviation. `ask` is the Agent SDK session. */
export async function screenHaiku(state: Record<string, unknown>, ask: typeof consult = consult,
  effort: Effort = "low"): Promise<number> {
  const { answer } = await ask<Record<string, unknown>>({
    cwd: process.cwd(),
    model: "haiku",
    effort,
    role: HAIKU_ROLE,
    schema: SCREEN_SCHEMA,
    maxTurns: 3,
    prompt: [
      "State:", "```json", JSON.stringify(state, null, 2), "```", "",
      "Statements:", `- ${SCREEN_NAME}: ${SCREEN_QUESTION}`,
    ].join("\n"),
  });
  return probability(answer?.[SCREEN_NAME], "haiku");
}

export interface Screening {
  backend: "jev" | "haiku";
  model: string;
  isDeviation: number;
}

/** Same order as `judge`: the configured model, then the fallback. Throws when both fail or are off. */
export async function screen(state: Record<string, unknown>, cfg: Triage, backends: Backends): Promise<Screening> {
  const order = [cfg.model, cfg.fallback].filter((m) => m !== "off");
  if (!order.length) throw new Error("triage is off");
  const errors: string[] = [];
  for (const model of order) {
    const backend = model === "haiku" ? "haiku" : "jev";
    const call = backend === "haiku" ? backends.screenHaiku : backends.screenJev;
    try {
      if (!call) throw new Error(`no ${backend} backend`);
      return { backend, model, isDeviation: await call(state) };
    } catch (e) {
      console.error(`triage: screening with ${model} failed:`, e);
      errors.push(`${model}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  throw new Error(`screening failed (${errors.join("; ")})`);
}

/** Tries the configured model, then the fallback. Throws when both fail or both are off: a failed
 * call is never turned into an answer, so nothing defaults to accept. */
export async function judge(state: Record<string, unknown>, cfg: Triage, backends: Backends): Promise<Judgment> {
  const order = [cfg.model, cfg.fallback].filter((m) => m !== "off");
  if (!order.length) throw new Error("triage is off");
  const errors: string[] = [];
  for (const model of order) {
    const backend = model === "haiku" ? "haiku" : "jev";
    const call = backends[backend];
    try {
      if (!call) throw new Error(`no ${backend} backend`);
      const answers = await call(state);
      return { backend, model, answers, verdict: verdict(answers, cfg.reject_at) };
    } catch (e) {
      console.error(`triage: ${model} failed:`, e);
      errors.push(`${model}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  throw new Error(`triage failed (${errors.join("; ")})`);
}

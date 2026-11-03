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

/** Asked only when the state has `human_choices`: a decision that repeats one is the human's choice. */
export const REPEATS_QUESTION =
  "Does this decision repeat a choice the human already made (one of `human_choices`), so that undoing it would undo the human?";
const REPEATS_NAME = "repeats_human_choice";

export type QuestionName = keyof typeof QUESTIONS;
export type Answers = Record<QuestionName, number> & { repeats_human_choice?: number };
export type Verdict = "accept" | "reject" | "escalate";

const NAMES = Object.keys(QUESTIONS) as QuestionName[];

type HumanChoice = { id?: unknown };

function humanChoices(state: Record<string, unknown>): HumanChoice[] {
  return Array.isArray(state.human_choices) ? state.human_choices as HumanChoice[] : [];
}

/** Whether the state carries choices the human made on the feature, so the repeat question applies. */
export function hasHumanChoices(state: Record<string, unknown>): boolean {
  return humanChoices(state).length > 0;
}

/** The human choice a repeat is reported against: the first the state lists (the latest). */
export function humanChoiceOf(state: Record<string, unknown>): number | undefined {
  const id = humanChoices(state)[0]?.id;
  return typeof id === "number" ? id : undefined;
}

/** A decision that repeats a human choice (at or above `rejectAt`) is the human's choice: accepted,
 * whatever the other flags say. Then a weakened test rejects first, coverage is never traded for a rule
 * conflict; then a rule conflict escalates to the human; then the other flags. Workaround counts only
 * when the plan did not ask for it; `narrower` is recorded, not used. */
export function verdict(answers: Answers, rejectAt: number): Verdict {
  if (repeatsHumanChoice(answers, rejectAt)) return "accept";
  if (answers.drops_coverage >= rejectAt) return "reject";
  if (answers.rule_conflict >= rejectAt) return "escalate";
  const workaround = answers.required_by_plan < rejectAt && answers.workaround >= rejectAt;
  return answers.contradicts >= rejectAt || workaround ? "reject" : "accept";
}

export function repeatsHumanChoice(answers: Answers, rejectAt: number): boolean {
  return answers.repeats_human_choice !== undefined && answers.repeats_human_choice >= rejectAt;
}

/** The questions asked of a state: the repeat question only when it has human choices. */
function questionNames(state: Record<string, unknown>): (QuestionName | typeof REPEATS_NAME)[] {
  return hasHumanChoices(state) ? [...NAMES, REPEATS_NAME] : NAMES;
}

const questionText = (n: QuestionName | typeof REPEATS_NAME): string => n === REPEATS_NAME ? REPEATS_QUESTION : QUESTIONS[n];

/** Throws unless every question asked has a number in 0..1. */
function validated(raw: unknown, from: string, names: readonly string[] = NAMES): Answers {
  const a = (raw ?? {}) as Record<string, unknown>;
  for (const n of names) {
    const v = a[n];
    if (typeof v !== "number" || !Number.isFinite(v) || v < 0 || v > 1) {
      throw new Error(`${from}: answer "${n}" is missing or outside 0..1`);
    }
  }
  return Object.fromEntries(names.map((n) => [n, a[n] as number])) as Answers;
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
  const names = questionNames(state);
  return validated(await askJev(state, names.map((name) => ({ name, question: questionText(name) })), key), "jev", names);
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

const haikuSchema = (names: readonly string[]) => ({
  type: "object",
  properties: {
    answers: {
      type: "object",
      properties: Object.fromEntries(names.map((n) => [n, { type: "number", minimum: 0, maximum: 1 }])),
      required: names,
    },
  },
  required: ["answers"],
});

/** `ask` is the Agent SDK session; tests inject a fake. */
export async function haiku(state: Record<string, unknown>, ask: typeof consult = consult,
  effort: Effort = "low", onCost?: (cost: number) => void): Promise<Answers> {
  const names = questionNames(state);
  const { answer, cost } = await ask<{ answers?: unknown }>({
    cwd: process.cwd(),
    model: "haiku",
    effort,
    role: HAIKU_ROLE,
    schema: haikuSchema(names),
    maxTurns: 3,
    prompt: [
      "State:", "```json", JSON.stringify(state, null, 2), "```", "",
      "Statements:", ...names.map((n) => `- ${n}: ${questionText(n)}`),
    ].join("\n"),
  });
  onCost?.(cost);
  return validated(answer?.answers, "haiku", names);
}

export interface Judgment {
  backend: "jev" | "haiku";
  model: string;
  answers: Answers;
  verdict: Verdict;
  /** The decision repeats a choice the human made: accepted as theirs, the other flags not judged. */
  humanChoice?: boolean;
}

/** What the screening asks: `is_deviation`, and `repeats_human_choice` when the state has human choices. */
export interface ScreenAnswers { is_deviation: number; repeats_human_choice?: number }

export type ScreenBackend = (state: Record<string, unknown>) => Promise<ScreenAnswers>;

export interface Backends {
  jev?: Backend;
  haiku?: Backend;
  /** The one-question screening, through the same models; tests inject fakes. */
  screenJev?: ScreenBackend;
  screenHaiku?: ScreenBackend;
}

/** The screening's questions: the repeat question rides in the same call, only with human choices. */
function screenNames(state: Record<string, unknown>): string[] {
  return hasHumanChoices(state) ? [SCREEN_NAME, REPEATS_NAME] : [SCREEN_NAME];
}

const screenText = (n: string): string => n === REPEATS_NAME ? REPEATS_QUESTION : SCREEN_QUESTION;

function screenAnswers(raw: unknown, from: string, names: string[]): ScreenAnswers {
  const a = (raw ?? {}) as Record<string, unknown>;
  for (const n of names) {
    const v = a[n];
    if (typeof v !== "number" || !Number.isFinite(v) || v < 0 || v > 1) {
      throw new Error(`${from}: answer "${n}" is missing or outside 0..1`);
    }
  }
  return names.includes(REPEATS_NAME)
    ? { is_deviation: a[SCREEN_NAME] as number, repeats_human_choice: a[REPEATS_NAME] as number }
    : { is_deviation: a[SCREEN_NAME] as number };
}

/** Jev's probability that the decision is a deviation (and that it repeats a human choice, when the
 * state has some). Throws on a failed or malformed call. */
export async function screenJev(state: Record<string, unknown>, key: string, ask: typeof askJev = askJev): Promise<ScreenAnswers> {
  const names = screenNames(state);
  const raw = await ask(state, names.map((name) => ({ name, question: screenText(name) })), key);
  return screenAnswers(raw, "jev", names);
}

const screenSchema = (names: string[]) => ({
  type: "object",
  properties: Object.fromEntries(names.map((n) => [n, { type: "number", minimum: 0, maximum: 1 }])),
  required: names,
});

/** Haiku's answers to the screening questions. `ask` is the Agent SDK session. */
export async function screenHaiku(state: Record<string, unknown>, ask: typeof consult = consult,
  effort: Effort = "low"): Promise<ScreenAnswers> {
  const names = screenNames(state);
  const { answer } = await ask<Record<string, unknown>>({
    cwd: process.cwd(),
    model: "haiku",
    effort,
    role: HAIKU_ROLE,
    schema: screenSchema(names),
    maxTurns: 3,
    prompt: [
      "State:", "```json", JSON.stringify(state, null, 2), "```", "",
      "Statements:", ...names.map((n) => `- ${n}: ${screenText(n)}`),
    ].join("\n"),
  });
  return screenAnswers(answer, "haiku", names);
}

export interface Screening {
  backend: "jev" | "haiku";
  model: string;
  isDeviation: number;
  /** Set only when the state had human choices. */
  repeatsHumanChoice?: number;
  /** The human choice the repeat is reported against. */
  humanChoiceOf?: number;
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
      const a = await call(state);
      return { backend, model, isDeviation: a.is_deviation,
        ...(a.repeats_human_choice === undefined ? {} : { repeatsHumanChoice: a.repeats_human_choice, humanChoiceOf: humanChoiceOf(state) }) };
    } catch (e) {
      console.error(`triage: screening with ${model} failed:`, e);
      errors.push(`${model}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  throw new Error(`screening failed (${errors.join("; ")})`);
}

/** Questions on a preflight's revision: the state carries the plan's `original` and `revised` tasks,
 * its `out_of_scope` and its `design`. */
export const REVISION_QUESTIONS = {
  drops_acceptance: "A task in `revised` drops or weakens an acceptance criterion that the same task in `original` had.",
  adds_out_of_scope: "The tasks in `revised` add work that the plan's `out_of_scope` rules out.",
  changes_design: "The tasks in `revised` go another way than a choice in the plan's `design`.",
} as const;

export type RevisionQuestionName = keyof typeof REVISION_QUESTIONS;
export type RevisionAnswers = Record<RevisionQuestionName, number>;
export type RevisionVerdict = "accept" | "hold";

export interface RevisionJudgment { model: string; answers: RevisionAnswers; verdict: RevisionVerdict }

/** Jev judges a revision: `hold` when any answer reaches `reject_at`, else `accept`. Only Jev, with a
 * key; no key, another model, a failed call or a missing or out-of-range answer throws, so a revision
 * never becomes `accept` by default. */
export async function judgeRevision(state: Record<string, unknown>, cfg: Triage, key: string | undefined,
  ask: typeof askJev = askJev): Promise<RevisionJudgment> {
  return holdOrAccept("revision", state, cfg, key, REVISION_QUESTIONS, ask) as Promise<RevisionJudgment>;
}

/** The staff's revision of one running task: the state carries that task's `original` and `revised`
 * plus the plan's `out_of_scope` and `design`. Same questions and verdict rules as `judgeRevision`. */
export async function judgeTaskRevision(state: Record<string, unknown>, cfg: Triage, key: string | undefined,
  ask: typeof askJev = askJev): Promise<RevisionJudgment> {
  return holdOrAccept("task revision", state, cfg, key, REVISION_QUESTIONS, ask) as Promise<RevisionJudgment>;
}

/** Questions on a task the staff closed as already satisfied: the state carries the `task` and the
 * `delivered` diff of the commit that already reached main. */
export const SATISFIED_QUESTIONS = {
  unmet_acceptance: "Some acceptance criterion of `task` is not met by the code in `delivered` (the diff of the commit that already reached main).",
  different_task: "The commit in `delivered` solves a different problem than `task`.",
} as const;

export type SatisfiedAnswers = Record<keyof typeof SATISFIED_QUESTIONS, number>;
export type SatisfiedVerdict = "accept" | "hold";

export interface SatisfiedJudgment { model: string; answers: SatisfiedAnswers; verdict: SatisfiedVerdict }

/** Jev judges a close as already satisfied: `hold` when either answer reaches `reject_at`, else
 * `accept`. Only Jev, with a key; anything else throws, never a verdict. */
export async function judgeSatisfied(state: Record<string, unknown>, cfg: Triage, key: string | undefined,
  ask: typeof askJev = askJev): Promise<SatisfiedJudgment> {
  return holdOrAccept("satisfied", state, cfg, key, SATISFIED_QUESTIONS, ask) as Promise<SatisfiedJudgment>;
}

async function holdOrAccept(what: string, state: Record<string, unknown>, cfg: Triage, key: string | undefined,
  questions: Record<string, string>, ask: typeof askJev): Promise<{ model: string; answers: Record<string, number>; verdict: "accept" | "hold" }> {
  if (cfg.model !== JEV_MODEL) throw new Error(`${what}: triage model is "${cfg.model}", only ${JEV_MODEL} judges this`);
  if (!key) throw new Error(`${what}: no Jev key`);
  const names = Object.keys(questions);
  const raw = (await ask(state, names.map((name) => ({ name, question: questions[name] })), key) ?? {}) as Record<string, unknown>;
  for (const n of names) {
    const v = raw[n];
    if (typeof v !== "number" || !Number.isFinite(v) || v < 0 || v > 1) {
      throw new Error(`jev: answer "${n}" is missing or outside 0..1`);
    }
  }
  const answers = Object.fromEntries(names.map((n) => [n, raw[n] as number]));
  const hold = names.some((n) => answers[n] >= cfg.reject_at);
  return { model: String((raw as { model?: unknown }).model ?? JEV_MODEL), answers, verdict: hold ? "hold" : "accept" };
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
      const humanChoice = repeatsHumanChoice(answers, cfg.reject_at);
      return { backend, model, answers, verdict: verdict(answers, cfg.reject_at), ...(humanChoice ? { humanChoice } : {}) };
    } catch (e) {
      console.error(`triage: ${model} failed:`, e);
      errors.push(`${model}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  throw new Error(`triage failed (${errors.join("; ")})`);
}

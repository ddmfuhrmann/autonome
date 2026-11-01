// Runs the triage in the run: judges the deviations waiting in the context and posts each verdict.
// It never stops the run and never blocks a task: an item that fails stays proposed for the next run.
import type { EventStream } from "./events.js";
import {
  context, type GateConfig, type RevisionItem, type RevisionVerdictBody, type ScreeningBody, type TriageItem,
  type TriageVerdictBody,
} from "./context.js";
import { jevKey } from "./secrets.js";
import {
  askJev, haiku, jev, judge, judgeRevision, screen, screenHaiku, screenJev,
  type Answers, type Backends, type Judgment, type RevisionAnswers, type RevisionJudgment, type RevisionQuestionName,
  type Screening,
} from "./triage.js";

export interface TriageRunOptions {
  log: (line: string) => void;
  events?: EventStream;
  /** Tests inject fakes; by default Jev (when a key is found) and Haiku. */
  backends?: Backends;
}

const FLAGS = ["drops_coverage", "contradicts", "workaround", "rule_conflict"] as const;

/** `triage D12: reject (jev, contradicts 0.91)`: the strongest flag is what decided, or came closest.
 * An escalate says so: `triage D12: escalate (jev, rule_conflict 0.91): a rule conflict for you`. */
export function verdictLine(decisionId: number, j: Judgment): string {
  const top = j.verdict === "escalate"
    ? "rule_conflict"
    : FLAGS.reduce((a, b) => (j.answers[b] > j.answers[a] ? b : a));
  const line = `triage D${decisionId}: ${j.verdict} (${j.backend}, ${top} ${j.answers[top]})`;
  return j.verdict === "escalate" ? `${line}: a rule conflict for you` : line;
}

const nouls = (a: Answers | RevisionAnswers): Record<string, string> =>
  Object.fromEntries(Object.entries(a).map(([k, v]) => [k, String(v)]));

// What the runner POSTs to the context. Each body is checked against contracts/runner-context.json by
// both test suites, so the two sides cannot drift apart while each tests against its own fake.

export function verdictBody(item: TriageItem, j: Judgment): TriageVerdictBody {
  return { backend: j.backend, model: j.model, state: JSON.stringify(item.state),
    answers: nouls(j.answers), verdict: j.verdict, round: item.round };
}

export function screeningBody(s: Screening, rejectAt: number): ScreeningBody {
  return { backend: s.backend, model: s.model, is_deviation: s.isDeviation, reject_at: rejectAt };
}

export function revisionBody(item: RevisionItem, j: RevisionJudgment): RevisionVerdictBody {
  return { model: j.model, state: JSON.stringify(item.state), answers: nouls(j.answers), verdict: j.verdict };
}

/** `triage P677: accept (jev, changes_design 0.13)`: the question with the highest answer. */
export function revisionLine(planId: number, j: RevisionJudgment): string {
  const names = Object.keys(j.answers) as RevisionQuestionName[];
  const top = names.reduce((a, b) => (j.answers[b] > j.answers[a] ? b : a));
  return `triage P${planId}: ${j.verdict} (jev, ${top} ${j.answers[top]})`;
}

/** Judges the preflight revisions waiting on a held plan; returns the ids of the plans it accepted.
 * A revision that changed shape is left for the human without calling Jev; a failure (no key included)
 * leaves the plan held. It never throws, so it never stops the run. */
export async function triageRevisions(project: string, cfg: GateConfig["config"], opts: TriageRunOptions & {
  /** Tests inject a fake of Jev's API. */
  ask?: typeof askJev;
}): Promise<number[]> {
  const triage = cfg.triage;
  const accepted: number[] = [];
  if (!triage || triage.model === "off") return accepted;
  let waiting: RevisionItem[];
  try {
    waiting = (await context.triageState(project)).revisions ?? [];
  } catch (e) {
    opts.log(`triage: could not read what is waiting: ${String(e)}`);
    return accepted;
  }
  if (!waiting.length) return accepted;
  let key: string | undefined;
  for (const item of waiting) {
    if (item.shape_changed) {
      opts.log(`triage P${item.plan_id}: shape changed, left for you`);
      continue;
    }
    try {
      key ??= jevKey((line) => opts.log(`triage: ${line}`));
      const j = await judgeRevision(item.state, triage, key, opts.ask);
      await context.triageRevision(item.plan_id, revisionBody(item, j));
      const line = revisionLine(item.plan_id, j);
      opts.log(line);
      opts.events?.push({ role: "runner", kind: "stage", text: line });
      if (j.verdict === "accept") accepted.push(item.plan_id);
    } catch (e) {
      opts.log(`triage P${item.plan_id}: left for you: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  return accepted;
}

export async function triagePending(project: string, cfg: GateConfig["config"], opts: TriageRunOptions): Promise<void> {
  const triage = cfg.triage;
  if (!triage || triage.model === "off") return;
  let waiting: Awaited<ReturnType<typeof context.triageState>>;
  try {
    waiting = await context.triageState(project);
  } catch (e) {
    return opts.log(`triage: could not read what is waiting: ${String(e)}`);
  }
  let items = waiting.deviations;
  const toScreen = waiting.screen ?? [];
  if (!items.length && !toScreen.length) return;
  const key = jevKey((line) => opts.log(`triage: ${line}`));
  const backends: Backends = opts.backends ?? {
    jev: key ? (state) => jev(state, key) : undefined, haiku,
    screenJev: key ? (state) => screenJev(state, key) : undefined, screenHaiku: (state) => screenHaiku(state),
  };
  if (toScreen.length) {
    let reopened = false;
    for (const item of toScreen) {
      try {
        const s = await screen(item.state, triage, backends);
        const r = await context.triageScreen(item.decision_id, screeningBody(s, triage.reject_at));
        reopened ||= r.deviation;
        const line = `triage screen D${item.decision_id}: ${s.isDeviation} (${s.backend})${r.deviation ? " → deviation" : ""}`;
        opts.log(line);
        opts.events?.push({ role: "runner", kind: "stage", text: line });
      } catch (e) {
        opts.log(`triage screen D${item.decision_id}: left unscreened: ${e instanceof Error ? e.message : String(e)}`);
      }
    }
    if (reopened) {
      try {
        items = (await context.triageState(project)).deviations;
      } catch (e) {
        return opts.log(`triage: could not read what is waiting: ${String(e)}`);
      }
    }
  }
  for (const item of items) {
    try {
      const j = await judge(item.state, triage, backends);
      await context.triageVerdict(item.decision_id, verdictBody(item, j));
      const line = verdictLine(item.decision_id, j);
      opts.log(line);
      opts.events?.push({ role: "runner", kind: "stage", text: line });
    } catch (e) {
      opts.log(`triage D${item.decision_id}: left proposed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
}

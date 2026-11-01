// Runs the triage in conduct: judges the deviations waiting in the context and posts each verdict.
// It never stops conduct and never blocks a task: an item that fails stays proposed for the next run.
import type { EventStream } from "./events.js";
import { context, type GateConfig } from "./context.js";
import { jevKey } from "./secrets.js";
import { haiku, jev, judge, screen, screenHaiku, screenJev, type Answers, type Backends, type Judgment } from "./triage.js";

export interface TriageRunOptions {
  log: (line: string) => void;
  events?: EventStream;
  /** Tests inject fakes; by default Jev (when a key is found) and Haiku. */
  backends?: Backends;
}

const FLAGS = ["drops_coverage", "contradicts", "workaround"] as const;

/** `triage D12: reject (jev, contradicts 0.91)`: the strongest flag is what decided, or came closest. */
export function verdictLine(decisionId: number, j: Judgment): string {
  const top = FLAGS.reduce((a, b) => (j.answers[b] > j.answers[a] ? b : a));
  return `triage D${decisionId}: ${j.verdict} (${j.backend}, ${top} ${j.answers[top]})`;
}

const nouls = (a: Answers): Record<string, string> =>
  Object.fromEntries(Object.entries(a).map(([k, v]) => [k, String(v)]));

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
        const r = await context.triageScreen(item.decision_id, {
          backend: s.backend, model: s.model, is_deviation: s.isDeviation, reject_at: triage.reject_at });
        reopened ||= r.deviation;
        const line = `triage screen D${item.decision_id}: ${s.isDeviation} (${s.backend})${r.deviation ? " → deviation" : ""}`;
        opts.log(line);
        opts.events?.push({ role: "maestro", kind: "stage", text: line });
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
      await context.triageVerdict(item.decision_id, {
        backend: j.backend, model: j.model, state: JSON.stringify(item.state),
        answers: nouls(j.answers), verdict: j.verdict, round: item.round,
      });
      const line = verdictLine(item.decision_id, j);
      opts.log(line);
      opts.events?.push({ role: "maestro", kind: "stage", text: line });
    } catch (e) {
      opts.log(`triage D${item.decision_id}: left proposed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
}

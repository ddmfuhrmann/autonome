// Runs the triage in conduct: judges the deviations waiting in the context and posts each verdict.
// It never stops conduct and never blocks a task: an item that fails stays proposed for the next run.
import type { EventStream } from "./events.js";
import { context, type GateConfig, type TriageItem } from "./context.js";
import { jevKey } from "./secrets.js";
import { haiku, jev, judge, type Answers, type Backends, type Judgment } from "./triage.js";

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
  let items: TriageItem[];
  try {
    items = await context.triageState(project);
  } catch (e) {
    return opts.log(`triage: could not read what is waiting: ${String(e)}`);
  }
  if (!items.length) return;
  const key = jevKey((line) => opts.log(`triage: ${line}`));
  const backends: Backends = opts.backends ?? { jev: key ? (state) => jev(state, key) : undefined, haiku };
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

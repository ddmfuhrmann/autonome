// autonome watch: one line per change worth knowing, polled from the context without any model.
// A Claude session runs it in the background (/autonome:watch) and only wakes up on a new line.
import { call } from "./context.js";

export interface State {
  tasks: { id: number; title: string; status: string; reason?: string }[];
  plans: { id: number; title: string; status: string }[];
  waves: { id: number; title: string; status: string }[];
  waiting: { kind: string; id: number; title: string; blocks: boolean; deviation?: boolean; exhausted?: boolean; conflict?: boolean;
    /** A held plan the triage judged `hold`: the question that weighed most. */
    triage_hold?: string | null }[];
  /** The triage is on: it judges deviations, so only the ones it gave up on wait for the human. */
  triage?: boolean;
}

type Waiting = State["waiting"][number];

/** The line for a waiting item, or null when the human has nothing to do with it. */
function waitingLine(i: Waiting, s: State): string | null {
  const tag = i.blocks ? "needs-you" : "review";
  if (i.kind === "held" && i.triage_hold) return `${tag} #${i.id} held, triage hold: ${i.triage_hold}`;
  if (i.kind === "decision" && i.deviation && s.triage) {
    if (i.conflict) return `review #${i.id} rule conflict: ${i.title}`;
    return i.exhausted ? `${tag} #${i.id} deviation the triage could not settle: ${i.title}` : null;
  }
  return `${tag} #${i.id} ${WAITING[i.kind] ?? i.kind}: ${i.title}`;
}

const WAITING: Record<string, string> = {
  plans: "to approve", held: "held by rehearsal", soundcheck: "blocked at the soundcheck", fermata: "held at the fermata",
  blocked: "blocked", playback: "merged, playback to hear", decision: "decision to review", convention: "convention proposed",
};

/** What changed between two snapshots, as lines tagged `needs-you`, `review` or `info`. */
export function changes(prev: State, next: State): string[] {
  const out: string[] = [];
  const before = new Map(prev.tasks.map((t) => [t.id, t.status]));
  for (const t of next.tasks) {
    const was = before.get(t.id);
    if (was === t.status) continue;
    if (t.status === "playing") out.push(`info T-${t.id} started: ${t.title}`);
    else if (t.status === "done" && was) out.push(`info T-${t.id} merged: ${t.title}`);
    else if (t.status === "pending" && was === "playing") out.push(`info T-${t.id} back to pending: ${t.title}`);
  }
  const plansBefore = new Map(prev.plans.map((p) => [p.id, p.status]));
  for (const p of next.plans) {
    if (p.status === "done" && plansBefore.get(p.id) && plansBefore.get(p.id) !== "done") {
      out.push(`info P-${p.id} done: ${p.title}`);
    }
  }
  const wavesBefore = new Map(prev.waves.map((w) => [w.id, w.status]));
  for (const w of next.waves) {
    if (w.status === "done" && wavesBefore.get(w.id) && wavesBefore.get(w.id) !== "done") {
      out.push(`info W-${w.id} finished: ${w.title}`);
    }
  }
  const seen = new Set(prev.waiting.map((i) => `${i.kind}:${i.id}`));
  for (const i of next.waiting) {
    if (seen.has(`${i.kind}:${i.id}`)) continue;
    const line = waitingLine(i, next);
    if (!line) continue;
    const reason = i.kind === "blocked" ? next.tasks.find((t) => t.id === i.id)?.reason : "";
    out.push(line + (reason ? ` (${reason.split("\n")[0].slice(0, 160)})` : ""));
  }
  return out;
}

export function summary(s: State): string {
  const count = (status: string) => s.tasks.filter((t) => t.status === status).length;
  const blocking = s.waiting.filter((i) => i.blocks).length;
  return `watching: ${count("playing")} playing, ${count("pending")} pending, ${count("done")} done; `
    + `${blocking} waiting on you, ${s.waiting.length - blocking} to review when you like`;
}

/** Prints the summary and what waits on the human; with `once`, stops there, otherwise prints one line
 * per change until killed. */
export async function watch(opts: { project: string; every: number; once?: boolean; log: (line: string) => void }): Promise<void> {
  const get = () => call<State>(`/api/projects/${opts.project}/state`);
  let prev = await get();
  opts.log(summary(prev));
  for (const i of prev.waiting) {
    const line = waitingLine(i, prev);
    if (line) opts.log(line);
  }
  if (opts.once) return;
  for (;;) {
    await new Promise((r) => setTimeout(r, opts.every * 1000));
    const next = await get().catch(() => null);
    if (!next) continue; // the context restarting: try again next round
    for (const line of changes(prev, next)) opts.log(line);
    prev = next;
  }
}

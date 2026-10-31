// Cues: scheduled checks that find work nobody planned (a red main, outdated dependencies, docs that
// drifted from the code). They only queue tasks, into the maintenance plan a human approved once;
// the maestro plays them like any other task. Run this from cron, launchd or a scheduled agent.
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { AUTONOME_HOME, PLUGIN_ROOT, updateDocs } from "./conduct.js";
import { context, type Cue, type Feature } from "./context.js";
import { mainCheckout } from "./git.js";

export interface CueOptions {
  project: string;
  repo: string;
  force: boolean;
  log: (line: string) => void;
}

function tail(text: string, lines = 40): string {
  return text.trim().split("\n").slice(-lines).join("\n");
}

export function drifted(features: Feature[]): string[] {
  return features
    .filter((f) => f.handoffs > 0 && (!f.documented || (f.last_work_at ?? "") > (f.doc_updated_at ?? "")))
    .map((f) => f.slug);
}

async function runCue(cue: Cue, cwd: string, opts: CueOptions): Promise<string> {
  const source = `cue:${cue.name}`;
  if (cue.kind === "command") {
    if (!cue.run) return "no command";
    const r = spawnSync(cue.run, { shell: true, cwd, encoding: "utf8", timeout: 900_000 });
    const out = `${r.stdout}${r.stderr}`;
    if (r.status === 0 && !r.stdout.trim()) return "clean";
    const q = await context.enqueue(opts.project, {
      title: cue.task ?? `Act on the ${cue.name} check`,
      spec: `The scheduled check "${cue.name}" (\`${cue.run}\`) reported:\n\n${tail(out)}\n\nResolve what it reports.`,
      source,
    });
    return q.duplicate ? `already queued as #${q.task_id}` : `queued #${q.task_id} (maintenance plan is ${q.plan_status})`;
  }
  if (cue.kind === "main-red") {
    const r = spawnSync(join(PLUGIN_ROOT, "bin", "autonome-gates"), ["--all-files", "--json"], { cwd, encoding: "utf8" });
    const out = JSON.parse(r.stdout || "{}") as { passed?: boolean; outcomes?: { gate: string; passed: boolean; output: string }[] };
    if (out.passed) return "main is green";
    const failed = out.outcomes?.find((o) => !o.passed);
    const q = await context.enqueue(opts.project, {
      title: cue.task ?? `Fix main: ${failed?.gate ?? "gates"} failing`,
      spec: `On main, the gate "${failed?.gate}" fails:\n\n${tail(failed?.output ?? r.stderr)}\n\nFind the cause and fix it; do not weaken the gate.`,
      source,
    });
    return q.duplicate ? `already queued as #${q.task_id}` : `queued #${q.task_id}`;
  }
  const stale = drifted(await context.features(opts.project));
  for (const feature of stale) updateDocs(opts.repo, opts.project, ["--feature", feature], opts.log);
  if (stale.length) updateDocs(opts.repo, opts.project, ["--overview"], opts.log);
  return stale.length ? `refreshed ${stale.join(", ")}` : "docs up to date";
}

export async function cues(opts: CueOptions): Promise<void> {
  const cfg = (await context.gateConfig(opts.project)).config;
  const statePath = join(AUTONOME_HOME, "state", `cues-${opts.project}.json`);
  const state: Record<string, string> = existsSync(statePath) ? JSON.parse(readFileSync(statePath, "utf8")) : {};
  const cwd = mainCheckout(opts.repo) ?? opts.repo;
  for (const cue of cfg.cues ?? []) {
    const last = state[cue.name] ? Date.parse(state[cue.name]) : 0;
    if (!opts.force && Date.now() - last < cue.every_hours * 3_600_000) continue;
    opts.log(`cue ${cue.name}: ${await runCue(cue, cwd, opts)}`);
    state[cue.name] = new Date().toISOString();
  }
  mkdirSync(join(AUTONOME_HOME, "state"), { recursive: true });
  writeFileSync(statePath, JSON.stringify(state));
}

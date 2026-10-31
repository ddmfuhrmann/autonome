// The quality loop: when a wave finishes, the tune skill reads what it left behind (reviewer advisory
// findings, fix tasks, repeated gate failures, friction) and proposes conventions for the next
// compose to start from. It only proposes; the human approves on the Approvals page.
import { query } from "@anthropic-ai/claude-agent-sdk";
import { context, type Plan } from "./context.js";
import { mainCheckout } from "./git.js";

export interface TuneOptions {
  project: string;
  repo: string;
  model?: string;
  log: (line: string) => void;
}

const MEMORY = ["search", "insights_get", "conventions_get", "plan_get", "plan_list", "gate_config_get",
  "convention_propose", "plan_draft"];

function tunePrompt(wave: Plan, project: string): string {
  const features = (wave.body.features as { plan_id: number }[]).map((f) => `#${f.plan_id}`).join(", ");
  return `Run the autonome tune skill for project ${project}, focused on wave #${wave.id} "${wave.body.title}" ` +
    `(plans ${features}), which just finished. Nobody will answer questions: do not ask, decide.\n\n` +
    "Mine what this wave left behind: insights_get (review_findings, repeated failures, blocked tasks, " +
    "friction), the handoffs of its features (search with type handoff), and fix tasks that came from a " +
    "soundcheck. For each pattern that repeated, or that would have prevented a fix task or a blocking " +
    "finding, propose a short, checkable convention with convention_propose, citing the ids in " +
    "`evidence` and `why`. Never loosen an existing convention. At most one plan_draft for a gate change. " +
    "End with a summary of at most 8 lines: what you proposed and why, with ids.";
}

/** Tunes every finished wave not tuned yet. `run` is the session, injectable for tests. */
export async function tuneFinishedWaves(opts: TuneOptions, run = runTune): Promise<number> {
  const cfg = (await context.gateConfig(opts.project)).config;
  if (cfg.tune_after_wave === false) return 0;
  const waves = await context.untunedWaves(opts.project);
  for (const wave of waves) {
    opts.log(`wave #${wave.id} ${wave.body.title} finished: tuning the process from its evidence`);
    const summary = await run(wave, opts).catch((e) => `the tune session failed: ${String(e)}`);
    await context.waveTuned(wave.id, summary);
    opts.log(`  ${summary.split("\n")[0]}`);
  }
  return waves.length;
}

async function runTune(wave: Plan, opts: TuneOptions): Promise<string> {
  const { PLUGIN_ROOT } = await import("./conduct.js");
  let summary = "";
  for await (const msg of query({
    prompt: tunePrompt(wave, opts.project),
    options: {
      cwd: mainCheckout(opts.repo) ?? opts.repo,
      model: opts.model,
      permissionMode: "dontAsk",
      allowedTools: ["Read", "Glob", "Grep", "Skill",
        ...MEMORY.flatMap((t) => [`mcp__plugin_autonome_autonome__${t}`, `mcp__autonome__${t}`])],
      settingSources: ["project"],
      plugins: [{ type: "local", path: PLUGIN_ROOT }],
      // Not a work session: no handoff of its own, no metronome.
      env: { ...process.env, AUTONOME_HANDOFF_CHILD: "1", AUTONOME_PROJECT: opts.project } as Record<string, string>,
      maxTurns: 60,
    },
  })) {
    if (msg.type === "result") summary = msg.subtype === "success" ? msg.result : `stopped: ${msg.subtype}`;
  }
  return summary.trim() || "no proposals";
}

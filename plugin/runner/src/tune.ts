// The quality loop: when a wave finishes, the tune skill runs a retrospective on what it left behind
// and proposes four kinds of change: conventions and gates for the project, a plan for the debt still
// open, process config, and notes about autonome itself. It only proposes; the human decides.
import { query } from "@anthropic-ai/claude-agent-sdk";
import { context, languageRule, roleModel, type Effort, type Plan } from "./context.js";
import { mainCheckout } from "./git.js";
import { isUsageLimit } from "./limits.js";
import { sessionEnv } from "./secrets.js";

export interface TuneOptions {
  project: string;
  repo: string;
  model?: string;
  effort?: Effort;
  log: (line: string) => void;
}

const MEMORY = ["search", "insights_get", "conventions_get", "plan_get", "plan_list", "gate_config_get",
  "convention_propose", "plan_draft"];

function tunePrompt(wave: Plan, project: string): string {
  const features = (wave.body.features as { plan_id: number }[]).map((f) => `#${f.plan_id}`).join(", ");
  return `Run the autonome tune skill for project ${project}, focused on wave #${wave.id} "${wave.body.title}" ` +
    `(plans ${features}), which just finished. Nobody will answer questions: do not ask, decide.\n\n` +
    "Run the retrospective the skill describes on what this wave left behind: insights_get (review_findings, " +
    "loose_ends, encores, spent, heavy_conventions, repeated failures, blocked tasks, friction), the " +
    "handoffs of its features (search with type handoff), gate_config_get, and fix tasks that came from a " +
    "soundcheck. Make the four kinds of proposal the skill lists, each only when the evidence supports it. " +
    "Never loosen an existing convention or gate. End with the report in the skill's format: the four " +
    "headings, at most 25 lines in all, ids for every claim.";
}

/** Tunes every finished wave not tuned yet. `run` is the session, injectable for tests. */
export async function tuneFinishedWaves(opts: TuneOptions, run = runTune): Promise<number> {
  const cfg = (await context.gateConfig(opts.project)).config;
  if (cfg.tune_after_wave === false) return 0;
  const waves = await context.untunedWaves(opts.project);
  for (const wave of waves) {
    opts.log(`wave #${wave.id} ${wave.body.title} finished: tuning the process from its evidence`);
    let summary: string;
    try {
      summary = await run(wave, opts);
    } catch (e) {
      if (isUsageLimit(String(e))) {
        opts.log(`  usage limit; wave #${wave.id} will be tuned on a later run`);
        continue;
      }
      summary = `the tune session failed: ${String(e)}`;
    }
    await context.waveTuned(wave.id, summary);
    opts.log(`  ${summary.split("\n")[0]}`);
  }
  return waves.length;
}

async function runTune(wave: Plan, opts: TuneOptions): Promise<string> {
  const { PLUGIN_ROOT } = await import("./conduct.js");
  const cfg = (await context.gateConfig(opts.project)).config;
  let summary = "";
  for await (const msg of query({
    prompt: [tunePrompt(wave, opts.project), languageRule(cfg)].filter(Boolean).join("\n\n"),
    options: {
      cwd: mainCheckout(opts.repo) ?? opts.repo,
      ...roleModel(cfg, "tune", opts),
      permissionMode: "dontAsk",
      allowedTools: ["Read", "Glob", "Grep", "Skill",
        ...MEMORY.flatMap((t) => [`mcp__plugin_autonome_autonome__${t}`, `mcp__autonome__${t}`])],
      settingSources: ["project"],
      plugins: [{ type: "local", path: PLUGIN_ROOT }],
      // Not a work session: no handoff of its own, no metronome.
      env: sessionEnv({ AUTONOME_HANDOFF_CHILD: "1", AUTONOME_PROJECT: opts.project }),
      maxTurns: 60,
    },
  })) {
    if (msg.type === "result") summary = msg.subtype === "success" ? msg.result : `stopped: ${msg.subtype}`;
  }
  return summary.trim() || "no proposals";
}

// The soundcheck: once every task of a plan has merged, the feature is tried in a running environment.
// Scripted checks run first, then an agent (the listener) exercises the plan's acceptance criteria on
// the live app. A failure comes back as fix tasks for the same plan.
import { spawnSync } from "node:child_process";
import { consult } from "./agents.js";
import { isUsageLimit } from "./limits.js";
import type { Plan, RoleModel, Soundcheck, SoundcheckResult } from "./context.js";

const LISTENER_PROMPT = `You are the listener of the autonome process. A feature was just merged, and the app
is running with synthetic data. Try it the way a user or a client would, against the URL you are given:
exercise each acceptance criterion of the plan on the running app (curl the endpoints, follow redirects,
check status codes and bodies, error cases), then poke at features next to it for regressions. Never
call real external services, never use real personal data, never change files.

- passed: true only if every acceptance criterion holds on the running app and every scripted check passed.
- report: what you exercised and what happened, briefly, with the requests that failed.
- fixes: for each real failure, one task that would fix it: the reproduction (request and response), what
  is wrong, where in the code it likely is, and as acceptance a test that reproduces it and must pass.
  Give fixes for failed scripted checks too. No fixes for things that work.`;

const SCHEMA = {
  type: "object",
  properties: {
    passed: { type: "boolean" },
    report: { type: "string" },
    fixes: {
      type: "array",
      items: {
        type: "object",
        properties: {
          title: { type: "string" },
          spec: { type: "string" },
          acceptance: { type: "array", items: { type: "string" } },
        },
        required: ["title", "spec", "acceptance"],
      },
    },
  },
  required: ["passed", "report", "fixes"],
} as const;

function sh(cmd: string, cwd: string, env: Record<string, string>, timeoutS = 900) {
  const r = spawnSync("sh", ["-c", cmd], { cwd, env: { ...process.env, ...env }, encoding: "utf8", timeout: timeoutS * 1000 });
  const output = `${r.stdout ?? ""}${r.stderr ?? ""}`.slice(-4000);
  return { ok: r.status === 0, output: r.error ? `${String(r.error)}\n${output}` : output };
}

export async function soundcheck(
  plan: Plan, cfg: Soundcheck, cwd: string, who: RoleModel & { language?: string } | undefined, log: (line: string) => void,
  onMessage?: (msg: unknown) => void,
): Promise<SoundcheckResult> {
  const env = { AUTONOME_APP_URL: cfg.url };
  const blocked = (report: string): SoundcheckResult => ({ passed: false, report, checks: [], fixes: [] });
  try {
    const up = sh(cfg.up, cwd, env);
    if (!up.ok) return blocked(`the environment did not start (${cfg.up}):\n${up.output}`);
    if (cfg.seed) {
      const seed = sh(cfg.seed, cwd, env);
      if (!seed.ok) return blocked(`the seed failed (${cfg.seed}):\n${seed.output}`);
    }
    const checks = cfg.checks.map((c) => {
      const r = sh(c.run, cwd, env, c.timeout_s);
      log(`  check ${c.name}: ${r.ok ? "passed" : "failed"}`);
      return { name: c.name, passed: r.ok, output: r.output };
    });
    const allChecks = checks.every((c) => c.passed);
    if (!cfg.listener) {
      return {
        passed: allChecks, checks,
        report: allChecks ? "all checks passed" : `failed: ${checks.filter((c) => !c.passed).map((c) => c.name).join(", ")}`,
        fixes: checks.filter((c) => !c.passed).map((c) => ({
          title: `Make the ${c.name} check pass`,
          spec: `The soundcheck check \`${c.name}\` failed against the running app:\n\n${c.output}`,
          acceptance: [`\`${c.name}\` passes`],
        })),
      };
    }
    const { title, goal, acceptance, tasks, out_of_scope } = plan.body as Record<string, unknown>;
    const { answer } = await consult<Omit<SoundcheckResult, "checks">>({
      cwd, ...who, onMessage, role: LISTENER_PROMPT, schema: SCHEMA, maxTurns: 60, env,
      tools: ["Bash(curl:*)"],
      prompt: [
        `The app runs at ${cfg.url}. Soundcheck plan #${plan.id} (feature ${plan.feature ?? "-"}).`,
        `## Plan\n${JSON.stringify({ title, goal, acceptance, out_of_scope, tasks }, null, 2)}`,
        `## Scripted checks\n${checks.map((c) => `### ${c.name}: ${c.passed ? "passed" : "FAILED"}\n${c.passed ? "" : c.output}`).join("\n") || "(none)"}`,
      ].join("\n\n"),
    });
    return { ...answer, passed: answer.passed && allChecks, checks };
  } catch (e) {
    if (isUsageLimit(String(e))) throw e; // not the feature's fault: the plan stays due
    return blocked(`the soundcheck itself failed: ${String(e)}`);
  } finally {
    const down = sh(cfg.down, cwd, env);
    if (!down.ok) log(`  environment teardown failed: ${down.output.slice(-300)}`);
  }
}

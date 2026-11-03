// The smoke test: once every task of a plan has merged, the feature is tried in a running environment.
// Scripted checks run first, then an agent (the tester) exercises the plan's acceptance criteria on
// the live app. A failure comes back as fix tasks for the same plan.
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { consult, type Consult } from "./agents.js";
import { bashPrefixes, scopedFileCommands } from "./fsguard.js";
import { tryGit } from "./git.js";
import { isUsageLimit } from "./limits.js";
import { HARD_DENIALS, SECRET_DENIALS } from "./run.js";
import { sessionEnv } from "./secrets.js";
import type { GateConfig, Plan, RoleModel, SmokeTest, SmokeTestResult } from "./context.js";

const TESTER_PROMPT = `You are the tester of the autonome process. A feature was just merged, and the app
is running with synthetic data. Try it the way a user or a client would, against the URL you are given:
exercise each acceptance criterion of the plan on the running app (curl the endpoints, follow redirects,
check status codes and bodies, error cases), then poke at features next to it for regressions. Never
call real external services, never use real personal data, never change files.

{{TOOLS}}

- passed: true only if nothing you exercised failed and every scripted check passed. A criterion you could
  not exercise does not make it false.
- untested: a criterion goes here only when it cannot be exercised on the running app (say why: no way to
  trigger it, needs a real external service, ...), never when you tried it and it failed. A failure goes
  in fixes and makes passed false. Empty when you exercised everything.
- report: what you exercised and what happened, briefly, with the requests that failed.
- fixes: for each real failure, one task that would fix it: the reproduction (request and response), what
  is wrong, where in the code it likely is, and as acceptance a test that reproduces it and must pass.
  Give fixes for failed scripted checks too. No fixes for things that work.`;

const TOOLS_BY_MODE = {
  allowlist: "Your tools: only plain `curl`, one request per call, no pipes; Read, Grep and Glob to understand the code.",
  auto: "A classifier decides what you may run; do not edit files, do not run the project's build or tests, test the running app.",
};

/** The tester's prompt, with what it can and cannot do in the project's permissions mode. */
export function testerPrompt(mode: "allowlist" | "auto"): string {
  return TESTER_PROMPT.replace("{{TOOLS}}", TOOLS_BY_MODE[mode]);
}

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
    untested: {
      type: "array",
      maxItems: 30,
      items: {
        type: "object",
        properties: { criterion: { type: "string", maxLength: 500 }, why: { type: "string", maxLength: 500 } },
        required: ["criterion", "why"],
      },
    },
  },
  required: ["passed", "report", "fixes", "untested"],
} as const;

// What the context accepts for `untested` (it answers 422 above these); the tester's answer is cut to fit.
const UNTESTED_MAX_ITEMS = 30;
const UNTESTED_MAX_CHARS = 500;

/** The result the context records: passed when the checks and the tester's run passed. `untested` is a note and
 * never enters `passed`. */
export function testerResult(answer: Omit<SmokeTestResult, "checks">, checks: SmokeTestResult["checks"]): SmokeTestResult {
  return {
    passed: answer.passed && checks.every((c) => c.passed),
    report: answer.report, checks, fixes: answer.fixes ?? [],
    untested: (answer.untested ?? []).slice(0, UNTESTED_MAX_ITEMS).map((u) => ({
      criterion: u.criterion.slice(0, UNTESTED_MAX_CHARS), why: u.why.slice(0, UNTESTED_MAX_CHARS),
    })),
  };
}

class TesterSetupError extends Error {}

const TESTER_TOOLS =["Read", "Grep", "Glob", "Bash(curl:*)"];

/** What the tester's session may do, in the project's permissions mode: it looks and calls the live app, and
 * never edits files, changes git history or reads `~/.autonome`. */
export function testerPermissions(gc: GateConfig, dir: string): NonNullable<Consult["permissions"]> {
  const disallowedTools = ["Edit", "Write", "NotebookEdit", ...HARD_DENIALS, ...SECRET_DENIALS];
  const hooks = { PreToolUse: [scopedFileCommands(dir, bashPrefixes(TESTER_TOOLS))] };
  if (gc.config.permissions === "auto") {
    return { permissionMode: "auto", permissionPrompts: "none", allowedTools: [], disallowedTools, hooks };
  }
  return { permissionMode: "dontAsk", allowedTools: TESTER_TOOLS, disallowedTools, hooks };
}

function sh(cmd: string, cwd: string, env: Record<string, string>, timeoutS = 900) {
  const r = spawnSync("sh", ["-c", cmd], { cwd, env: sessionEnv(env), encoding: "utf8", timeout: timeoutS * 1000 });
  const output = `${r.stdout ?? ""}${r.stderr ?? ""}`.slice(-4000);
  return { ok: r.status === 0, output: r.error ? `${String(r.error)}\n${output}` : output };
}

export async function smokeTest(
  plan: Plan, cfg: SmokeTest, cwd: string, who: RoleModel & { language?: string } | undefined, log: (line: string) => void,
  onMessage?: (msg: unknown) => void, extra: { gc?: GateConfig; query?: Consult["query"] } = {},
): Promise<SmokeTestResult> {
  const env = { AUTONOME_APP_URL: cfg.url };
  let scratch: string | undefined;
  const blocked = (report: string): SmokeTestResult => ({ passed: false, report, checks: [], fixes: [], untested: [] });
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
    if (!cfg.tester) {
      return {
        passed: allChecks, checks, untested: [],
        report: allChecks ? "all checks passed" : `failed: ${checks.filter((c) => !c.passed).map((c) => c.name).join(", ")}`,
        fixes: checks.filter((c) => !c.passed).map((c) => ({
          title: `Make the ${c.name} check pass`,
          spec: `The smoke test check \`${c.name}\` failed against the running app:\n\n${c.output}`,
          acceptance: [`\`${c.name}\` passes`],
        })),
      };
    }
    const { title, goal, acceptance, tasks, out_of_scope } = plan.body as Record<string, unknown>;
    // The tester reads a throwaway worktree of main, so nothing it does touches the checkout.
    scratch = mkdtempSync(join(tmpdir(), "autonome-tester-"));
    const added = tryGit(cwd, "worktree", "add", "--detach", scratch, "main");
    // Infrastructure, not the feature's fault: the plan stays due instead of blocking.
    if (!added.ok) throw new TesterSetupError(`could not create the tester's worktree: ${added.out}`);
    const gc = extra.gc ?? ({ version: null, config: {} } as unknown as GateConfig);
    const mode = gc.config.permissions === "auto" ? "auto" : "allowlist";
    const { answer } = await consult<Omit<SmokeTestResult, "checks">>({
      cwd: scratch, ...who, onMessage, role: testerPrompt(mode), schema: SCHEMA, maxTurns: 60, env,
      permissions: testerPermissions(gc, scratch), query: extra.query,
      prompt: [
        `The app runs at ${cfg.url}. Smoke test of plan #${plan.id} (feature ${plan.feature ?? "-"}).`,
        `## Plan\n${JSON.stringify({ title, goal, acceptance, out_of_scope, tasks }, null, 2)}`,
        `## Scripted checks\n${checks.map((c) => `### ${c.name}: ${c.passed ? "passed" : "FAILED"}\n${c.passed ? "" : c.output}`).join("\n") || "(none)"}`,
      ].join("\n\n"),
    });
    return testerResult(answer, checks);
  } catch (e) {
    if (e instanceof TesterSetupError || isUsageLimit(String(e))) throw e; // not the feature's fault: the plan stays due
    return blocked(`the smoke test itself failed: ${String(e)}`);
  } finally {
    if (scratch) {
      tryGit(cwd, "worktree", "remove", "--force", scratch);
      rmSync(scratch, { recursive: true, force: true });
    }
    const down = sh(cfg.down, cwd, env);
    if (!down.ok) log(`  environment teardown failed: ${down.output.slice(-300)}`);
  }
}

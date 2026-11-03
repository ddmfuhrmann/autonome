import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { GateConfig, Plan } from "./context.js";
import { conventionTitles, enforceConventions, findingsForRetry } from "./review.js";
import { smokeTest, testerPermissions, testerPrompt, testerResult } from "./smoke-test.js";

const plan: Plan = { id: 7, project: "p", status: "approved", feature: "links", body: { title: "Links", tasks: [] } };
const base = { url: "http://127.0.0.1:1", seed: null, tester: false, max_fixes: 2 };

test("scripted checks run against the environment, failures become fix tasks, and it is always torn down", async () => {
  const dir = mkdtempSync(join(tmpdir(), "smoke-test-"));
  const r = await smokeTest(plan, {
    ...base, up: "echo up > state", down: "echo down >> state",
    checks: [{ name: "smoke", run: 'test "$AUTONOME_APP_URL" = http://127.0.0.1:1', timeout_s: 10 },
             { name: "e2e", run: "echo 'GET /abc -> 500'; exit 1", timeout_s: 10 }],
  }, dir, undefined, () => {});
  assert.equal(r.passed, false);
  assert.deepEqual(r.checks.map((c) => [c.name, c.passed]), [["smoke", true], ["e2e", false]]);
  assert.equal(r.fixes.length, 1);
  assert.match(r.fixes[0].spec, /GET \/abc -> 500/);
  assert.equal(readFileSync(join(dir, "state"), "utf8"), "up\ndown\n");
});

test("an environment that does not start blocks with nothing to fix, and is still torn down", async () => {
  const dir = mkdtempSync(join(tmpdir(), "smoke-test-"));
  const r = await smokeTest(plan, { ...base, up: "echo nope; exit 3", down: "echo down > state", checks: [] },
    dir, undefined, () => {});
  assert.equal(r.passed, false);
  assert.equal(r.fixes.length, 0);
  assert.match(r.report, /did not start[\s\S]*nope/);
  assert.equal(readFileSync(join(dir, "state"), "utf8"), "down\n");
});

test("the Jev key does not reach the smoke test commands", async () => {
  const dir = mkdtempSync(join(tmpdir(), "smoke-test-"));
  const was = process.env.TYPESAFE_API_KEY;
  process.env.TYPESAFE_API_KEY = "secret";
  try {
    const r = await smokeTest(plan, {
      ...base, up: 'test -z "$TYPESAFE_API_KEY"', down: 'test -z "$TYPESAFE_API_KEY"', seed: 'test -z "$TYPESAFE_API_KEY"',
      checks: [{ name: "env", run: 'test -z "$TYPESAFE_API_KEY"', timeout_s: 10 }],
    }, dir, undefined, () => {});
    assert.equal(r.passed, true, r.report);
  } finally {
    if (was === undefined) delete process.env.TYPESAFE_API_KEY;
    else process.env.TYPESAFE_API_KEY = was;
  }
});

test("only high findings go back to the implementer", () => {
  const text = findingsForRetry({ verdict: "fail", findings: [
    { severity: "high", file: "a.py", issue: "redirects to any scheme, including javascript:" },
    { severity: "low", issue: "name could be clearer" },
  ] });
  assert.equal(text, "- a.py: redirects to any scheme, including javascript:");
});

test("a missing finding against an active convention blocks, whatever severity the reviewer gave it", () => {
  const briefing = "# task\n## What to do\nx\n## Conventions for these layers\n### Response models and string errors\nno raw 500\n### Tests assert behaviour\ny\n## Gates\nz";
  const titles = conventionTitles(briefing);
  assert.deepEqual(titles, ["Response models and string errors", "Tests assert behaviour"]);
  const out = enforceConventions([
    { kind: "missing", severity: "low", issue: "five collisions raise RuntimeError: a raw 500, which the Response models and string errors convention forbids" },
    { kind: "missing", severity: "medium", issue: "an edge nobody asked for" },
    { kind: "diverges", severity: "low", convention: "Tests assert behaviour", issue: "style of a test" },
  ], titles);
  assert.deepEqual(out.map((f) => [f.severity, f.convention]), [
    ["high", "Response models and string errors"], ["medium", undefined], ["low", "Tests assert behaviour"]]);
  assert.match(out[0].issue, /blocking: required by the convention "Response models and string errors"/);
});

const gcWith = (permissions?: string) => ({ version: 1, config: { permissions } }) as unknown as GateConfig;

function repo(): string {
  const dir = mkdtempSync(join(tmpdir(), "smoke-repo-"));
  const run = (...a: string[]) => execFileSync("git", a, { cwd: dir, stdio: "ignore" });
  run("init", "-q", "-b", "main");
  writeFileSync(join(dir, "f"), "x");
  run("add", ".");
  run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init");
  return dir;
}

const answer = { passed: true, report: "ok", fixes: [], untested: [] };
const withTester = { ...base, tester: true, up: "true", down: "true", checks: [] };

test("in auto the tester cannot edit, commit or read ~/.autonome, by rule", () => {
  const p = testerPermissions(gcWith("auto"), "/wt");
  assert.equal(p.permissionMode, "auto");
  assert.equal(p.permissionPrompts, "none");
  for (const d of ["Edit", "Write", "NotebookEdit", "Bash(git commit:*)", "Bash(git push:*)", "Read(~/.autonome/**)", "Bash(cat ~/.autonome:*)"]) {
    assert.ok(p.disallowedTools.includes(d), d);
  }
  assert.equal(p.hooks?.PreToolUse?.[0].matcher, "Bash");
});

test("in allowlist the tester has Read, Grep, Glob and curl, and the same denials", () => {
  for (const mode of ["allowlist", undefined]) {
    const p = testerPermissions(gcWith(mode), "/wt");
    assert.equal(p.permissionMode, "dontAsk");
    assert.deepEqual(p.allowedTools, ["Read", "Grep", "Glob", "Bash(curl:*)"]);
    assert.ok(p.disallowedTools.includes("Write"));
    assert.ok(p.disallowedTools.includes("Bash(git commit:*)"));
  }
});

test("the tester runs in a throwaway worktree of main, removed afterwards even when the session throws", async () => {
  for (const throws of [false, true]) {
    const dir = repo();
    let seen: any;
    const fake = (async function* (args: { options: unknown }) {
      seen = args.options;
      assert.ok(existsSync(join(seen.cwd, "f")));
      if (throws) throw new Error("boom");
      yield { type: "result", subtype: "success", total_cost_usd: 0, structured_output: answer };
    }) as never;
    const r = await smokeTest(plan, withTester, dir, undefined, () => {}, undefined, { gc: gcWith("auto"), query: fake });
    assert.notEqual(seen.cwd, dir);
    assert.equal(seen.permissionMode, "auto");
    assert.equal(existsSync(seen.cwd), false);
    assert.doesNotMatch(execFileSync("git", ["worktree", "list"], { cwd: dir, encoding: "utf8" }), /autonome-tester/);
    assert.equal(r.passed, !throws);
  }
});

test("the tester's prompt says what each mode can and cannot do", () => {
  assert.match(testerPrompt("allowlist"), /only plain `curl`, one request per call, no pipes/);
  assert.match(testerPrompt("allowlist"), /Read, Grep and Glob/);
  assert.match(testerPrompt("auto"), /classifier decides/);
  assert.match(testerPrompt("auto"), /do not edit files, do not run the project's build or tests/);
  assert.doesNotMatch(testerPrompt("auto"), /\{\{TOOLS\}\}/);
});

test("a tester worktree that cannot be created throws instead of failing the feature, and the environment is torn down", async () => {
  const dir = mkdtempSync(join(tmpdir(), "smoke-nogit-"));
  const fake = (async function* () { throw new Error("must not run"); }) as never;
  await assert.rejects(
    smokeTest(plan, { ...withTester, up: "true", down: "echo down > state" }, dir, undefined, () => {}, undefined,
      { gc: gcWith("auto"), query: fake }),
    /could not create the tester's worktree/);
  assert.equal(readFileSync(join(dir, "state"), "utf8"), "down\n");
});

const testerSays = (a: object) => (async function* () {
  yield { type: "result", subtype: "success", total_cost_usd: 0, structured_output: a };
}) as never;
const untested = [{ criterion: "email is sent", why: "needs a real SMTP server" }, { criterion: "cron runs", why: "no way to trigger it" }];

test("a tester that passed with criteria it could not exercise passes the smoke test and posts them as untested", async () => {
  const dir = repo();
  const r = await smokeTest(plan, withTester, dir, undefined, () => {}, undefined,
    { gc: gcWith("auto"), query: testerSays({ ...answer, untested }) });
  assert.equal(r.passed, true);
  assert.deepEqual(r.untested, untested);
  assert.deepEqual(r.fixes, []);
});

test("a real failure fails the smoke test with its fix, untested or not", async () => {
  const dir = repo();
  const fix = { title: "Fix redirect", spec: "GET /abc -> 500", acceptance: ["a test passes"] };
  const r = await smokeTest(plan, withTester, dir, undefined, () => {}, undefined,
    { gc: gcWith("auto"), query: testerSays({ passed: false, report: "bad", fixes: [fix], untested: untested.slice(0, 1) }) });
  assert.equal(r.passed, false);
  assert.deepEqual(r.fixes, [fix]);
  assert.equal(r.untested.length, 1);
});

test("untested is cut to what the context accepts: 30 items of 500 characters", () => {
  const long = { criterion: "c".repeat(501), why: "w".repeat(501) };
  const r = testerResult({ passed: true, report: "ok", fixes: [], untested: Array(31).fill(long) }, []);
  assert.equal(r.untested.length, 30);
  assert.equal(r.untested[0].criterion.length, 500);
  assert.equal(r.untested[0].why.length, 500);
  assert.equal(r.passed, true);
});

test("the tester's prompt keeps untested for what cannot be exercised, never for a failure", () => {
  assert.match(testerPrompt("auto"), /untested: a criterion goes here only when it cannot be exercised/);
  assert.match(testerPrompt("auto"), /never when you tried it and it failed/);
});

test("in every mode the tester's hook denies file commands outside its worktree and leaves curl to the rules", async () => {
  for (const mode of ["allowlist", "auto"]) {
    const hook = testerPermissions(gcWith(mode), "/wt").hooks!.PreToolUse![0].hooks[0];
    const ask = async (command: string) =>
      ((await hook({ tool_input: { command } } as never, undefined, { signal: new AbortController().signal })) as any)
        .hookSpecificOutput?.permissionDecision;
    assert.equal(await ask("rm /etc/x"), "deny", mode);
    assert.equal(await ask("cp /etc/passwd /wt/x"), "deny", mode);
    assert.equal(await ask("curl -s http://127.0.0.1:1/health"), undefined, mode);
  }
});

test("in every mode the tester's session denies ~/.autonome by rule", () => {
  for (const mode of ["allowlist", "auto"]) {
    const p = testerPermissions(gcWith(mode), "/wt");
    for (const d of ["Read(~/.autonome/**)", "Edit(~/.autonome/**)", "Bash(cat ~/.autonome:*)"]) {
      assert.ok(p.disallowedTools.includes(d), `${mode}: ${d}`);
    }
  }
});

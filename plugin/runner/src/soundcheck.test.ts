import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { Plan } from "./context.js";
import { conventionTitles, enforceConventions, findingsForEncore } from "./review.js";
import { soundcheck } from "./soundcheck.js";

const plan: Plan = { id: 7, project: "p", status: "approved", feature: "links", body: { title: "Links", tasks: [] } };
const base = { url: "http://127.0.0.1:1", seed: null, listener: false, max_fixes: 2 };

test("scripted checks run against the environment, failures become fix tasks, and it is always torn down", async () => {
  const dir = mkdtempSync(join(tmpdir(), "soundcheck-"));
  const r = await soundcheck(plan, {
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
  const dir = mkdtempSync(join(tmpdir(), "soundcheck-"));
  const r = await soundcheck(plan, { ...base, up: "echo nope; exit 3", down: "echo down > state", checks: [] },
    dir, undefined, () => {});
  assert.equal(r.passed, false);
  assert.equal(r.fixes.length, 0);
  assert.match(r.report, /did not start[\s\S]*nope/);
  assert.equal(readFileSync(join(dir, "state"), "utf8"), "down\n");
});

test("only high findings go back to the musician", () => {
  const text = findingsForEncore({ verdict: "fail", findings: [
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

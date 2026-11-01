import assert from "node:assert/strict";
import { test } from "node:test";
import { verdictLine } from "./conduct.js";
import { earlierFindingsSection, encoreMessage } from "./review.js";

test("the reviewer's verdict reads as a short list in the live view", () => {
  assert.equal(verdictLine({ verdict: "pass", findings: [] }), "**pass**, no findings");
  assert.equal(verdictLine({ verdict: "pass", findings: [
    { kind: "missing", severity: "low", issue: "No note on\nthe check.", file: "gradle/libs.versions.toml" }] }),
    "**pass**, 1 finding:\n- low missing in `gradle/libs.versions.toml`: No note on the check.");
});

test("medium findings go back while rounds remain; high ones always; low ones never", () => {
  const f = (severity: "high" | "medium" | "low", issue: string) => ({ kind: "missing" as const, severity, issue });
  const mediumOnly = { verdict: "pass" as const, findings: [f("medium", "weak test"), f("low", "nit")] };
  assert.match(encoreMessage(mediumOnly, true)!, /passed your diff[\s\S]*- weak test$/);
  assert.ok(!encoreMessage(mediumOnly, true)!.includes("nit"));
  assert.equal(encoreMessage(mediumOnly, false), null); // rounds spent: it becomes a loose end at merge
  assert.equal(encoreMessage({ verdict: "pass", findings: [f("low", "nit")] }, true), null);
  const both = encoreMessage({ verdict: "fail", findings: [f("high", "stray file"), f("medium", "weak test")] }, false)!;
  assert.match(both, /blocking issues[\s\S]*stray file[\s\S]*Also worth fixing now[\s\S]*weak test/);
});

test("a later review round gets the earlier findings to check, and no new medium ones", () => {
  assert.equal(earlierFindingsSection(undefined), "");
  const s = earlierFindingsSection([{ kind: "missing", severity: "medium", issue: "weak test", file: "A.java" }]);
  assert.match(s, /^## Earlier findings[\s\S]*- medium missing in A\.java: weak test[\s\S]*only new high findings/);
});

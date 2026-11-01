import assert from "node:assert/strict";
import { test } from "node:test";
import { verdictLine } from "./conduct.js";
import type { Finding, ReviewResult, Run } from "./context.js";
import { earlierFindingsSection, encoreMessage, recordRound, settleOutcomes, settleRun } from "./review.js";

test("every round keeps its findings, each marked with what became of it", () => {
  const f = (severity: "high" | "medium", issue: string): Finding => ({ kind: "missing", severity, issue });
  const rounds: ReviewResult[] = [
    { verdict: "fail", findings: [f("high", "no test"), f("high", "raw 500")] },
    { verdict: "pass", findings: [f("high", "Raw 500 ")] },
  ];
  settleOutcomes(rounds, "merged");
  assert.deepEqual(rounds[0].findings.map((x) => x.outcome), ["resolved", "repeated"]);
  assert.deepEqual(rounds[1].findings.map((x) => x.outcome), ["merged"]);
  settleOutcomes(rounds, "blocked");
  assert.equal(rounds[1].findings[0].outcome, "blocked");
});

test("the run keeps each round as recorded and review stays the last; outcomes settle on the run", () => {
  const f = (issue: string): Finding => ({ kind: "missing", severity: "high", issue });
  const r1: ReviewResult = { verdict: "fail", findings: [f("no test")] };
  const r2: ReviewResult = { verdict: "pass", findings: [] };
  const run: Run = { at: "", branch: "", worktree: "", base: "", outcome: "" };
  recordRound(run, r1, 1);
  recordRound(run, r2, 2);
  assert.deepEqual(run.reviews, [r1, r2]);
  assert.equal(run.review, r2);
  assert.equal(run.review_rounds, 2);
  settleRun(run, "merged");
  assert.equal(r1.findings[0].outcome, "resolved"); // fixed in round 2: not reported again
  settleRun({}, "merged"); // review off: nothing to settle
});

test("a block that is not the reviewer's leaves the last round's findings unmarked", () => {
  const medium: Finding = { kind: "missing", severity: "medium", issue: "weak test" };
  const run: Run = { at: "", branch: "", worktree: "", base: "", outcome: "" };
  recordRound(run, { verdict: "pass", findings: [medium] }, 1);
  settleRun(run, undefined); // e.g. the merge failed
  assert.equal(medium.outcome, undefined);
});

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

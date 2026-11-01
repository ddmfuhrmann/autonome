import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { judgeTask, recordRunnerError, verdictLine, type ConductOptions, type JudgeSeams } from "./conduct.js";
import type { Finding, GateConfig, ReviewResult, Run, Task } from "./context.js";
import { earlierFindingsSection, encoreMessage, gatesSection, recordRound, settleOutcomes, settleRun } from "./review.js";

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

/** Runs the real review loop of conduct over a temp repo, with a scripted reviewer and fake gates. */
async function runLoop(rounds: ReviewResult[], maxRounds: number, briefings: string[] = []) {
  const dir = mkdtempSync(join(tmpdir(), "judge-"));
  const sh = (...a: string[]) => execFileSync("git", a, { cwd: dir, encoding: "utf8" }).trim();
  sh("init", "-q", "-b", "main");
  sh("config", "user.email", "t@t");
  sh("config", "user.name", "t");
  writeFileSync(join(dir, "a.txt"), "one\n");
  sh("add", "-A");
  sh("commit", "-q", "-m", "base");
  const base = sh("rev-parse", "HEAD");
  writeFileSync(join(dir, "a.txt"), "two\n");

  const run: Run = { at: "", branch: "", worktree: dir, base, outcome: "" };
  const task = { id: 1, project: "p", body: { title: "t", spec: "s" } } as unknown as Task;
  const cfg = { review: { enabled: true, max_rounds: maxRounds }, encore: { max: 1 }, models: {} } as unknown as GateConfig["config"];
  const opts = { project: "p", repo: dir, maxTasks: 1, push: false, log: () => {} } as ConductOptions;
  let call = 0;
  let encores = 0;
  const seams: JudgeSeams = {
    gates: () => ({ passed: true, summary: "all gates passed", outcomes: [
      { gate: "lint", passed: true, output: "" },
      { gate: "test", passed: true, output: "12 passed\nTOTAL    210    9   96%\n" },
    ] }),
    briefing: async () => "briefing",
    // A fresh copy per call, as a real reviewer returns: rounds never share an object.
    review: (async (_task: Task, briefing: string) => {
      briefings.push(briefing);
      return { result: structuredClone(rounds[Math.min(call++, rounds.length - 1)]), cost: 0 };
    }) as JudgeSeams["review"],
  };
  const judged = await judgeTask({ task, cfg, opts, run, worktree: dir, base, current: () => ({ sessionId: "", stop: "" }),
    encore: async () => { encores++; } }, seams);
  return { run, judged, encores: () => encores };
}

test("conduct's review loop keeps every round; fixed findings resolve and the last round's merge", async () => {
  const f = (severity: "high" | "medium" | "low", issue: string): Finding => ({ kind: "missing", severity, issue });
  const r1: ReviewResult = { verdict: "fail", findings: [f("high", "no test")] };
  const r2: ReviewResult = { verdict: "pass", findings: [f("medium", "weak name")] };
  const r3: ReviewResult = { verdict: "pass", findings: [f("low", "nit")] };
  const { run, judged, encores } = await runLoop([r1, r2, r3], 3);
  assert.equal(judged.failure, null);
  assert.equal(judged.blockedOnReview, false);
  assert.equal(encores(), 2); // round 1 high, round 2 medium once; round 3 only has a low one
  const rounds = run.reviews!;
  assert.equal(rounds.length, 3);
  assert.equal(new Set(rounds).size, 3); // each round is its own result
  assert.deepEqual(rounds.map((r) => r.findings[0].issue), ["no test", "weak name", "nit"]);
  assert.equal(run.review, rounds[2]);
  assert.equal(run.review_rounds, 3);
  settleRun(run, "merged");
  assert.deepEqual(rounds.map((r) => r.findings[0].outcome), ["resolved", "resolved", "merged"]);
});

test("conduct's review loop blocks when the reviewer keeps failing until max_rounds", async () => {
  const bad: Finding = { kind: "missing", severity: "high", issue: "raw 500" };
  const fail = (): ReviewResult => ({ verdict: "fail", findings: [{ ...bad }] });
  const { run, judged } = await runLoop([fail(), fail(), fail()], 2);
  assert.match(judged.failure!, /still blocking after 2 encore/);
  assert.equal(judged.blockedOnReview, true);
  assert.equal(run.reviews?.length, 3);
  settleRun(run, judged.blockedOnReview ? "blocked" : undefined);
  assert.deepEqual(run.reviews?.slice(0, 2).map((r) => r.findings[0].outcome), ["repeated", "repeated"]);
  assert.equal(run.review?.findings[0].outcome, "blocked");
});

test("the reviewer gets the gates the runner just ran, so it does not try to run them", async () => {
  const briefings: string[] = [];
  await runLoop([{ verdict: "pass", findings: [] }], 2, briefings);
  assert.equal(briefings.length, 1);
  assert.match(briefings[0], /^briefing\n\n## Gates \(already run on this diff\)\n/);
  assert.match(briefings[0], /- lint: passed\n- test: passed: TOTAL {4}210 {4}9 {3}96%$/);
});

test("the gates section is empty without outcomes and keeps one line per gate", () => {
  assert.equal(gatesSection(undefined), "");
  assert.equal(gatesSection([]), "");
  const long = "x".repeat(500);
  assert.equal(gatesSection([{ gate: "mypy", passed: false, output: `\n${long}\n\n` }]),
    `## Gates (already run on this diff)\n- mypy: failed: ${"x".repeat(200)}`);
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

function errorsFor(outcomes: string[]) {
  const run = (outcome: string): Run => ({ at: "", branch: "", worktree: "", base: "", outcome });
  const task = { id: 7, body: { title: "t", runs: outcomes.map(run) } } as unknown as Task;
  const calls: { to: string; run?: Run }[] = [];
  const transition = async (_id: number, to: string, run?: Run) => { calls.push({ to, run }); };
  return { task, calls, transition };
}

test("a runner exception sends the task back to pending with the error run recorded", async () => {
  const { task, calls, transition } = errorsFor([]);
  const to = await recordRunnerError(task, new TypeError("x.map is not a function"), transition);
  assert.equal(to, "pending");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].to, "pending");
  assert.equal(calls[0].run?.outcome, "error");
  assert.match(calls[0].run!.reason!, /TypeError: x\.map is not a function/);
});

test("the third runner exception in a row blocks the task", async () => {
  const { task, calls, transition } = errorsFor(["error", "error"]);
  const to = await recordRunnerError(task, new TypeError("boom"), transition);
  assert.equal(to, "blocked");
  assert.equal(calls[0].to, "blocked");
  assert.equal(calls[0].run?.outcome, "error");
  assert.match(calls[0].run!.reason!, /^runner error \(3 in a row\): TypeError: boom/);
});

test("a merged run between two errors resets the count", async () => {
  const { task, calls, transition } = errorsFor(["error", "merged", "error"]);
  assert.equal(await recordRunnerError(task, new Error("boom"), transition), "pending");
  assert.equal(calls[0].to, "pending");
});

test("a later review round gets the earlier findings to check, and no new medium ones", () => {
  assert.equal(earlierFindingsSection(undefined), "");
  const s = earlierFindingsSection([{ kind: "missing", severity: "medium", issue: "weak test", file: "A.java" }]);
  assert.match(s, /^## Earlier findings[\s\S]*- medium missing in A\.java: weak test[\s\S]*only new high findings/);
});

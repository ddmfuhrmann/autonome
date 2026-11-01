import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import type { consult } from "./agents.js";
import { triageCase, triageDeviationsCase, triageRevisionsCase, triageRevisionsRun } from "./bench.js";
import { judgeRevision, type Answers, type askJev } from "./triage.js";

const FIXTURE = new URL("../bench/triage-deviations.json", import.meta.url);
const cases = JSON.parse(readFileSync(FIXTURE, "utf8")) as { id: string; expected: string; state: { deviation: { title: string } } }[];

const answers = (flag: number, rule_conflict = 0): Answers =>
  ({ drops_coverage: flag, contradicts: 0, workaround: 0, required_by_plan: 0, narrower: 0, rule_conflict });

test("the fixture has the 7 real shortlink deviations: 1 rejected, 2 escalated, 4 accepted", () => {
  assert.equal(cases.length, 7);
  assert.deepEqual(cases.filter((c) => c.expected === "reject").map((c) => c.id), ["shortlink-v5-594"]);
  assert.deepEqual(cases.filter((c) => c.expected === "escalate").map((c) => c.id), ["shortlink-180", "shortlink-4-217"]);
  assert.equal(cases.filter((c) => c.expected === "accept").length, 4);
});

test("the fixture carries no confere-v2 names", () => {
  assert.doesNotMatch(readFileSync(FIXTURE, "utf8"), /confere/i);
});

test("a fake backend that answers the expected verdicts scores 1.0", async () => {
  const byTitle = new Map(cases.map((c) => [c.state.deviation.title, c.expected]));
  const r = await triageDeviationsCase(async (s) => {
    const want = byTitle.get((s as typeof cases[0]["state"]).deviation.title);
    return answers(want === "reject" ? 0.9 : 0.1, want === "escalate" ? 0.9 : 0);
  });
  assert.equal(r.score, 1);
  assert.equal(r.note, "7/7");
});

test("triageCase sums Haiku's cost over the 7 cases into the score; Jev's stays 0", async () => {
  const ask = (async () => ({ answer: { answers: answers(0.1) }, cost: 0.01 })) as unknown as typeof consult;
  const r = await triageCase({ model: "haiku", effort: "low" }, ask);
  assert.ok(Math.abs(r.cost - 0.07) < 1e-9);
  assert.match(r.note, /^4\/7/);
});

const REVISIONS = new URL("../bench/triage-revisions.json", import.meta.url);
const revisions = JSON.parse(readFileSync(REVISIONS, "utf8")) as { id: string; expected: string; state: Record<string, unknown> }[];
const jevCfg = { model: "jev-latest", fallback: "off", reject_at: 0.7, max_rounds: 1 } as const;

test("the revisions fixture has the 5 known cases and no confere-v2 names", () => {
  assert.deepEqual(revisions.map((c) => [c.id, c.expected]), [
    ["shortlink-v5-P532", "accept"], ["shortlink-v6-P677", "accept"],
    ["shortlink-v6-P677-no-criteria", "hold"], ["shortlink-v6-P677-rate-limit", "hold"], ["shortlink-v6-P677-async", "hold"],
  ]);
  assert.doesNotMatch(readFileSync(REVISIONS, "utf8"), /confere/i);
});

test("a fake Jev that answers the expected verdicts scores 5/5 through judgeRevision", async () => {
  const byState = new Map(revisions.map((c) => [JSON.stringify(c.state), c.expected]));
  const ask = (async (state: Record<string, unknown>) => {
    const hold = byState.get(JSON.stringify(state)) === "hold";
    return { drops_acceptance: hold ? 0.9 : 0.1, adds_out_of_scope: 0.1, changes_design: 0.1 };
  }) as unknown as typeof askJev;
  const r = await triageRevisionsCase((state) => judgeRevision(state, jevCfg, "k", ask));
  assert.equal(r.score, 1);
  assert.equal(r.note, "5/5");
});

test("revision misses are listed, a failing judgment is raised, Haiku is skipped", async () => {
  const r = await triageRevisionsCase(async () => ({ verdict: "accept" }));
  assert.equal(r.score, 2 / 5);
  assert.match(r.note, /shortlink-v6-P677-async \(accept, expected hold\)/);
  await assert.rejects(triageRevisionsCase(async () => { throw new Error("boom"); }), /boom/);
  const h = await triageRevisionsRun({ model: "haiku", effort: "low" });
  assert.equal(h.score, 0);
  assert.match(h.note, /^skipped/);
});

test("misses are listed and a failing backend is raised, not scored", async () => {
  const r = await triageDeviationsCase(async () => answers(0.1));
  assert.equal(r.score, 4 / 7);
  assert.match(r.note, /shortlink-v5-594 \(accept, expected reject\)/);
  assert.match(r.note, /shortlink-180 \(accept, expected escalate\)/);
  await assert.rejects(triageDeviationsCase(async () => { throw new Error("boom"); }), /boom/);
});

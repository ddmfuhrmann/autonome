import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import type { consult } from "./agents.js";
import { triageCase, triageDeviationsCase } from "./bench.js";
import type { Answers } from "./triage.js";

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

test("misses are listed and a failing backend is raised, not scored", async () => {
  const r = await triageDeviationsCase(async () => answers(0.1));
  assert.equal(r.score, 4 / 7);
  assert.match(r.note, /shortlink-v5-594 \(accept, expected reject\)/);
  assert.match(r.note, /shortlink-180 \(accept, expected escalate\)/);
  await assert.rejects(triageDeviationsCase(async () => { throw new Error("boom"); }), /boom/);
});

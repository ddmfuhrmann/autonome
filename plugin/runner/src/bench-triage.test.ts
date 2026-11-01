import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { triageDeviationsCase } from "./bench.js";
import type { Answers } from "./triage.js";

const FIXTURE = new URL("../bench/triage-deviations.json", import.meta.url);
const cases = JSON.parse(readFileSync(FIXTURE, "utf8")) as { id: string; expected: string; state: { deviation: { title: string } } }[];

const answers = (flag: number): Answers =>
  ({ drops_coverage: flag, contradicts: 0, workaround: 0, required_by_plan: 0, narrower: 0, rule_conflict: 0 });

test("the fixture has the 7 real shortlink deviations, 6 accepted and 1 rejected", () => {
  assert.equal(cases.length, 7);
  assert.deepEqual(cases.filter((c) => c.expected === "reject").map((c) => c.id), ["shortlink-v5-594"]);
});

test("the fixture carries no confere-v2 names", () => {
  assert.doesNotMatch(readFileSync(FIXTURE, "utf8"), /confere/i);
});

test("a fake backend that answers the expected verdicts scores 1.0", async () => {
  const byTitle = new Map(cases.map((c) => [c.state.deviation.title, c.expected]));
  const r = await triageDeviationsCase(async (s) => answers(byTitle.get((s as typeof cases[0]["state"]).deviation.title) === "reject" ? 0.9 : 0.1));
  assert.equal(r.score, 1);
  assert.equal(r.note, "7/7");
});

test("misses are listed and a failing backend is raised, not scored", async () => {
  const r = await triageDeviationsCase(async () => answers(0.1));
  assert.equal(r.score, 6 / 7);
  assert.match(r.note, /shortlink-v5-594 \(accept, expected reject\)/);
  await assert.rejects(triageDeviationsCase(async () => { throw new Error("boom"); }), /boom/);
});

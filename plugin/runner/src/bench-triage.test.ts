import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { triageDeviationsCase } from "./bench.js";
import type { Answers } from "./triage.js";

const FIXTURE = new URL("../bench/triage-deviations.json", import.meta.url);
const cases = JSON.parse(readFileSync(FIXTURE, "utf8")) as { id: string; expected: string; state: { deviation: { title: string } } }[];

const answers = (flag: number): Answers =>
  ({ drops_coverage: flag, contradicts: 0, workaround: 0, required_by_plan: 0, narrower: 0 });

test("the fixture has 12 deviations, 10 accepted and 2 rejected", () => {
  assert.equal(cases.length, 12);
  assert.equal(cases.filter((c) => c.expected === "reject").length, 2);
});

test("the fixture carries no confere-v2 names", () => {
  assert.doesNotMatch(readFileSync(FIXTURE, "utf8"), /confere/i);
});

test("a fake backend that answers the expected verdicts scores 1.0", async () => {
  const byTitle = new Map(cases.map((c) => [c.state.deviation.title, c.expected]));
  const r = await triageDeviationsCase(async (s) => answers(byTitle.get((s as typeof cases[0]["state"]).deviation.title) === "reject" ? 0.9 : 0.1));
  assert.equal(r.score, 1);
  assert.equal(r.note, "12/12");
});

test("misses are listed and a failing backend is raised, not scored", async () => {
  const r = await triageDeviationsCase(async () => answers(0.1));
  assert.equal(r.score, 10 / 12);
  assert.match(r.note, /tracker-370 \(accept, expected reject\)/);
  await assert.rejects(triageDeviationsCase(async () => { throw new Error("boom"); }), /boom/);
});

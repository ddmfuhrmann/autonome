import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import type { consult } from "./agents.js";
import type { Triage } from "./context.js";
import { type Answers, haiku, jev, judge, QUESTIONS, verdict } from "./triage.js";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

const base: Answers = { drops_coverage: 0, contradicts: 0, workaround: 0, required_by_plan: 0, narrower: 0 };
const ans = (o: Partial<Answers>): Answers => ({ ...base, ...o });
const cfg: Triage = { model: "jev-latest", fallback: "haiku", reject_at: 0.7, max_rounds: 2 };
const state = { deviation: { title: "t" } };

test("verdict table", () => {
  assert.equal(verdict(ans({}), 0.7), "accept");
  assert.equal(verdict(ans({ drops_coverage: 0.7 }), 0.7), "reject");
  assert.equal(verdict(ans({ drops_coverage: 0.69 }), 0.7), "accept");
  assert.equal(verdict(ans({ contradicts: 0.9 }), 0.7), "reject");
  assert.equal(verdict(ans({ narrower: 1 }), 0.7), "accept");
  // #180: a workaround the plan asked for
  assert.equal(verdict(ans({ workaround: 0.9, required_by_plan: 0.9 }), 0.7), "accept");
  // #370: a workaround nobody asked for
  assert.equal(verdict(ans({ workaround: 0.9 }), 0.7), "reject");
  assert.equal(verdict(ans({ workaround: 0.9, required_by_plan: 0.5 }), 0.7), "reject");
  // required_by_plan excuses only the workaround
  assert.equal(verdict(ans({ drops_coverage: 0.9, required_by_plan: 0.9 }), 0.7), "reject");
});

test("jev failure falls back to haiku", async () => {
  const j = await judge(state, cfg, {
    jev: async () => { throw new Error("down"); },
    haiku: async () => ans({ contradicts: 0.8 }),
  });
  assert.equal(j.backend, "haiku");
  assert.equal(j.model, "haiku");
  assert.equal(j.verdict, "reject");
});

test("jev answers first when it works", async () => {
  const j = await judge(state, cfg, { jev: async () => ans({}), haiku: async () => { throw new Error("unused"); } });
  assert.deepEqual([j.backend, j.model, j.verdict], ["jev", "jev-latest", "accept"]);
});

test("both failing throws, never accepts", async () => {
  await assert.rejects(judge(state, cfg, {
    jev: async () => { throw new Error("down"); },
    haiku: async () => { throw new Error("bad"); },
  }), /triage failed/);
});

test("both off throws", async () => {
  await assert.rejects(judge(state, { ...cfg, model: "off", fallback: "off" }, {}), /off/);
});

test("model off with a fallback uses the fallback", async () => {
  const j = await judge(state, { ...cfg, model: "off" }, { haiku: async () => ans({}) });
  assert.equal(j.backend, "haiku");
});

function fakeJev(answers: Record<string, number>, calls: { url: string; body: any }[] = []) {
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(input), body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({ answers }), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  return calls;
}

test("a jev response missing one answer throws", async () => {
  const { narrower: _, ...missing } = ans({});
  fakeJev(missing);
  await assert.rejects(jev(state, "k"), /narrower/);
});

test("an out-of-range answer throws", async () => {
  fakeJev(ans({ workaround: 1.5 }));
  await assert.rejects(jev(state, "k"), /workaround/);
});

const askHaiku = (answer: unknown, seen: any[] = []) =>
  (async (c: unknown) => { seen.push(c); return { answer, cost: 0 }; }) as unknown as typeof consult;

test("haiku asks on the haiku/low role and returns valid answers", async () => {
  const seen: any[] = [];
  const got = await haiku(state, askHaiku({ answers: ans({ workaround: 0.4 }) }, seen));
  assert.equal(got.workaround, 0.4);
  assert.equal(seen[0].model, "haiku");
  assert.equal(seen[0].effort, "low");
});

test("a haiku answer missing one question throws", async () => {
  const { narrower: _, ...missing } = ans({});
  await assert.rejects(haiku(state, askHaiku({ answers: missing })), /narrower/);
});

test("an out-of-range haiku answer throws", async () => {
  await assert.rejects(haiku(state, askHaiku({ answers: ans({ contradicts: -0.1 }) })), /contradicts/);
  await assert.rejects(haiku(state, askHaiku({ answers: ans({ contradicts: 2 }) })), /contradicts/);
});

test("a haiku session with no answers object throws", async () => {
  await assert.rejects(haiku(state, askHaiku({})), /drops_coverage/);
});

test("a non-2xx jev response throws", async () => {
  globalThis.fetch = (async () => new Response("no", { status: 500 })) as typeof fetch;
  await assert.rejects(jev(state, "k"), /500/);
});

test("the request sends every question as a noul, with the state as an object", async () => {
  const calls = fakeJev(ans({}));
  await jev(state, "secret");
  assert.equal(calls.length, 1);
  const { body } = calls[0];
  assert.deepEqual(body.state, state);
  assert.deepEqual(body.nouls.map((n: any) => n.name).sort(), Object.keys(QUESTIONS).sort());
  for (const n of body.nouls) {
    assert.equal(n.type, "noul");
    assert.equal(n.question, QUESTIONS[n.name as keyof typeof QUESTIONS]);
  }
});

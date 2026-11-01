import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import type { consult } from "./agents.js";
import type { Triage } from "./context.js";
import { type Answers, haiku, jev, judge, judgeRevision, pingJev, QUESTIONS, REVISION_QUESTIONS,SCREEN_QUESTION, screen, screenJev, verdict } from "./triage.js";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

const base: Answers = { drops_coverage: 0, contradicts: 0, workaround: 0, required_by_plan: 0, narrower: 0, rule_conflict: 0 };
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

test("verdict table with the bench's shapes: a weakened test first, then a rule conflict", () => {
  assert.equal(verdict(ans({ drops_coverage: 0.7, rule_conflict: 0.9 }), 0.7), "reject"); // #180
  assert.equal(verdict(ans({ drops_coverage: 0.17, rule_conflict: 0.91, contradicts: 0.86 }), 0.7), "escalate"); // #217
  assert.equal(verdict(ans({ drops_coverage: 0.89, rule_conflict: 0.54 }), 0.7), "reject"); // #594
  assert.equal(verdict(ans({ drops_coverage: 0.93, rule_conflict: 0.73 }), 0.7), "reject"); // #439
  assert.equal(verdict(ans({ drops_coverage: 0.5, contradicts: 0.5, workaround: 0.5, rule_conflict: 0.5 }), 0.7), "accept");
  assert.equal(verdict(ans({ rule_conflict: 0.7 }), 0.7), "escalate");
  assert.equal(verdict(ans({ rule_conflict: 0.69, contradicts: 0.9 }), 0.7), "reject");
});

test("the Jev request carries rule_conflict as a noul and reads its answer", async () => {
  const calls = fakeJev(ans({ rule_conflict: 0.91 }));
  const got = await jev(state, "k");
  assert.equal(got.rule_conflict, 0.91);
  assert.deepEqual(calls[0].body.questions.rule_conflict, { type: "noul", instructions: QUESTIONS.rule_conflict });
});

test("a missing rule_conflict answer throws, from jev and from haiku", async () => {
  const { rule_conflict: _, ...missing } = ans({});
  fakeJev(missing);
  await assert.rejects(jev(state, "k"), /rule_conflict/);
  await assert.rejects(haiku(state, askHaiku({ answers: missing })), /rule_conflict/);
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

test("screen: jev, then haiku, never a default", async () => {
  const s = await screen(state, cfg, { screenJev: async () => { throw new Error("down"); }, screenHaiku: async () => 0.8 });
  assert.deepEqual([s.backend, s.model, s.isDeviation], ["haiku", "haiku", 0.8]);
  await assert.rejects(screen(state, cfg, { screenJev: async () => { throw new Error("down"); } }), /screening failed/);
});

test("screenJev sends the existing request shape with one noul and rejects a malformed answer", async () => {
  const asked: { nouls: { name: string; question: string }[] }[] = [];
  const p = await screenJev(state, "k", async (_s, nouls) => { asked.push({ nouls }); return { is_deviation: 0.9 }; });
  assert.equal(p, 0.9);
  assert.deepEqual(asked[0].nouls, [{ name: "is_deviation", question: SCREEN_QUESTION }]);
  await assert.rejects(screenJev(state, "k", async () => ({})), /is_deviation/);
});

test("both off throws", async () => {
  await assert.rejects(judge(state, { ...cfg, model: "off", fallback: "off" }, {}), /off/);
});

test("model off with a fallback uses the fallback", async () => {
  const j = await judge(state, { ...cfg, model: "off" }, { haiku: async () => ans({}) });
  assert.equal(j.backend, "haiku");
});

// Answers shaped like TypeSafe's real response. Recorded from the live API on 2026-11-01:
// {"model":"jev-1.14.0","answers":{"billing":{"type":"noul","noul":0.97}},"usage":{"input_tokens":277,"output_tokens":20}}
function fakeJev(answers: Record<string, number>, calls: { url: string; body: any; auth?: string }[] = []) {
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(input), body: JSON.parse(String(init?.body)),
      auth: (init?.headers as Record<string, string> | undefined)?.authorization });
    const shaped = Object.fromEntries(Object.entries(answers).map(([k, v]) => [k, { type: "noul", noul: v }]));
    return new Response(JSON.stringify({ model: "jev-1.14.0", answers: shaped, usage: { input_tokens: 277, output_tokens: 20 } }),
      { status: 200, headers: { "content-type": "application/json" } });
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

test("haiku passes consult's cost to onCost", async () => {
  const ask = (async () => ({ answer: { answers: ans({}) }, cost: 0.012 })) as unknown as typeof consult;
  const costs: number[] = [];
  await haiku(state, ask, "low", (c) => costs.push(c));
  assert.deepEqual(costs, [0.012]);
});

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

test("the request follows TypeSafe's System One API: endpoint, model, state and questions as nouls", async () => {
  const saved = process.env.TYPESAFE_URL;
  delete process.env.TYPESAFE_URL;
  try {
    const calls = fakeJev(ans({ workaround: 0.42 }));
    const got = await jev(state, "secret");
    assert.equal(got.workaround, 0.42); // read from answers.<name>.noul
    assert.equal(calls.length, 1);
    const { url, body, auth } = calls[0];
    assert.equal(url, "https://api.typesafe.ai/v1/systemone");
    assert.equal(auth, "Bearer secret");
    assert.equal(body.model, "jev-latest");
    assert.deepEqual(body.state, state);
    assert.deepEqual(Object.keys(body.questions).sort(), Object.keys(QUESTIONS).sort());
    for (const [name, q] of Object.entries(body.questions) as [keyof typeof QUESTIONS, any][]) {
      assert.deepEqual(q, { type: "noul", instructions: QUESTIONS[name] });
    }
  } finally {
    if (saved !== undefined) process.env.TYPESAFE_URL = saved;
  }
});

const revState = { original: [{ title: "t" }], revised: [{ title: "t" }], out_of_scope: [], design: [] };
const rev = (a: number, b: number, c: number) => ({ drops_acceptance: a, adds_out_of_scope: b, changes_design: c });

test("a revision is accepted below reject_at and held at or above it, for the measured scores", async () => {
  for (const [scores, want] of [
    [rev(0.06, 0.07, 0.09), "accept"], [rev(0.08, 0.08, 0.13), "accept"],
    [rev(0.93, 0.05, 0.05), "hold"], [rev(0.05, 0.88, 0.05), "hold"], [rev(0.05, 0.05, 0.94), "hold"],
  ] as const) {
    fakeJev(scores);
    const j = await judgeRevision(revState, cfg, "k");
    assert.equal(j.verdict, want);
    assert.deepEqual(j.answers, scores);
    assert.equal(j.model, "jev-1.14.0");
  }
});

test("the revision request goes to systemone with the three questions as nouls", async () => {
  const saved = process.env.TYPESAFE_URL;
  delete process.env.TYPESAFE_URL;
  try {
    const calls = fakeJev(rev(0.1, 0.1, 0.1));
    await judgeRevision(revState, cfg, "secret");
    assert.equal(calls.length, 1);
    const { url, body, auth } = calls[0];
    assert.equal(url, "https://api.typesafe.ai/v1/systemone");
    assert.equal(auth, "Bearer secret");
    assert.deepEqual(body.state, revState);
    assert.deepEqual(Object.keys(body.questions).sort(), Object.keys(REVISION_QUESTIONS).sort());
    for (const [name, q] of Object.entries(body.questions) as [keyof typeof REVISION_QUESTIONS, any][]) {
      assert.deepEqual(q, { type: "noul", instructions: REVISION_QUESTIONS[name] });
    }
  } finally {
    if (saved !== undefined) process.env.TYPESAFE_URL = saved;
  }
});

test("a revision with no key, a haiku model, an HTTP error or a bad answer throws, with no verdict", async () => {
  const calls = fakeJev(rev(0.1, 0.1, 0.1));
  await assert.rejects(judgeRevision(revState, cfg, undefined), /key/);
  await assert.rejects(judgeRevision(revState, { ...cfg, model: "haiku" }, "k"), /haiku/);
  assert.equal(calls.length, 0);
  globalThis.fetch = (async () => new Response("no", { status: 500 })) as typeof fetch;
  await assert.rejects(judgeRevision(revState, cfg, "k"), /500/);
  fakeJev({ drops_acceptance: 0.1, adds_out_of_scope: 0.1 });
  await assert.rejects(judgeRevision(revState, cfg, "k"), /changes_design/);
  fakeJev(rev(0.1, 1.5, 0.1));
  await assert.rejects(judgeRevision(revState, cfg, "k"), /adds_out_of_scope/);
});

test("pingJev proves the key and endpoint with one question and names the model that answered", async () => {
  const calls = fakeJev({ billing: 0.97 });
  assert.equal(await pingJev("k"), "jev-1.14.0");
  assert.deepEqual(Object.keys(calls[0].body.questions), ["billing"]);
  globalThis.fetch = (async () => new Response("not found", { status: 404 })) as typeof fetch;
  await assert.rejects(pingJev("k"), /HTTP 404/);
  fakeJev({});
  await assert.rejects(pingJev("k"), /billing/);
});

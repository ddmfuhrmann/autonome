import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import type { consult } from "./agents.js";
import type { Triage } from "./context.js";
import { type Answers, haiku, jev, judge, judgeRevision, judgeSatisfied, judgeRuleChange, judgeTaskRevision, pingJev, QUESTIONS, REPEATS_QUESTION, REVISION_QUESTIONS, RULE_QUESTIONS, SATISFIED_QUESTIONS, SCREEN_QUESTION, screen, screenHaiku, screenJev, verdict } from "./triage.js";

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
  const s = await screen(state, cfg, { screenJev: async () => { throw new Error("down"); }, screenHaiku: async () => ({ is_deviation: 0.8 }) });
  assert.deepEqual([s.backend, s.model, s.isDeviation], ["haiku", "haiku", 0.8]);
  await assert.rejects(screen(state, cfg, { screenJev: async () => { throw new Error("down"); } }), /screening failed/);
});

test("screenJev sends the existing request shape with one noul and rejects a malformed answer", async () => {
  const asked: { nouls: { name: string; question: string }[] }[] = [];
  const p = await screenJev(state, "k", async (_s, nouls) => { asked.push({ nouls }); return { is_deviation: 0.9 }; });
  assert.deepEqual(p, { is_deviation: 0.9 });
  assert.deepEqual(asked[0].nouls, [{ name: "is_deviation", question: SCREEN_QUESTION }]);
  await assert.rejects(screenJev(state, "k", async () => ({})), /is_deviation/);
});

const withChoices = { ...state, human_choices: [{ id: 634, title: "Sonnet", decision: "Reviewer on Sonnet.", status: "accepted" }] };

test("with human_choices the repeat question rides in the screening call and in the judgment call; without, it does not", async () => {
  const asked: string[][] = [];
  const ask = async (_s: Record<string, unknown>, nouls: { name: string }[]) => {
    asked.push(nouls.map((n) => n.name));
    return { is_deviation: 0.9, repeats_human_choice: 0.8 };
  };
  assert.deepEqual(await screenJev(withChoices, "k", ask), { is_deviation: 0.9, repeats_human_choice: 0.8 });
  await screenJev(state, "k", ask);
  assert.deepEqual(asked, [["is_deviation", "repeats_human_choice"], ["is_deviation"]]);

  const calls = fakeJev({ ...ans({}), repeats_human_choice: 0.9 });
  assert.equal((await jev(withChoices, "k")).repeats_human_choice, 0.9);
  assert.deepEqual(calls[0].body.questions.repeats_human_choice, { type: "noul", instructions: REPEATS_QUESTION });
  const plain = fakeJev(ans({}));
  assert.equal((await jev(state, "k")).repeats_human_choice, undefined);
  assert.ok(!("repeats_human_choice" in plain[0].body.questions));
});

test("a repeat of a human choice wins: accepted as theirs, even with drops_coverage at the same level", async () => {
  assert.equal(verdict({ ...ans({ drops_coverage: 0.9 }), repeats_human_choice: 0.9 }, 0.7), "accept");
  assert.equal(verdict({ ...ans({ drops_coverage: 0.9 }), repeats_human_choice: 0.69 }, 0.7), "reject");
  const j = await judge(withChoices, cfg, { jev: async () => ({ ...ans({ drops_coverage: 0.9, rule_conflict: 0.9 }), repeats_human_choice: 0.9 }) });
  assert.deepEqual([j.verdict, j.humanChoice], ["accept", true]);
  const low = await judge(withChoices, cfg, { jev: async () => ({ ...ans({ drops_coverage: 0.9 }), repeats_human_choice: 0.1 }) });
  assert.deepEqual([low.verdict, low.humanChoice], ["reject", undefined]);
});

test("a missing or out-of-range repeats_human_choice throws, from jev, haiku and the screening", async () => {
  fakeJev(ans({}));
  await assert.rejects(jev(withChoices, "k"), /repeats_human_choice/);
  fakeJev({ ...ans({}), repeats_human_choice: 1.5 });
  await assert.rejects(jev(withChoices, "k"), /repeats_human_choice/);
  await assert.rejects(haiku(withChoices, askHaiku({ answers: ans({}) })), /repeats_human_choice/);
  await assert.rejects(haiku(withChoices, askHaiku({ answers: { ...ans({}), repeats_human_choice: -1 } })), /repeats_human_choice/);
  await assert.rejects(screenJev(withChoices, "k", async () => ({ is_deviation: 0.9 })), /repeats_human_choice/);
  await assert.rejects(screenHaiku(withChoices, askHaiku({ is_deviation: 0.9, repeats_human_choice: 2 })), /repeats_human_choice/);
});

test("haiku asks the repeat question by the same schema, only with human_choices", async () => {
  const seen: any[] = [];
  const got = await haiku(withChoices, askHaiku({ answers: { ...ans({}), repeats_human_choice: 0.8 } }, seen));
  assert.equal(got.repeats_human_choice, 0.8);
  assert.ok(seen[0].schema.properties.answers.required.includes("repeats_human_choice"));
  assert.ok(seen[0].prompt.includes(REPEATS_QUESTION));
  await haiku(state, askHaiku({ answers: ans({}) }, seen));
  assert.ok(!seen[1].schema.properties.answers.required.includes("repeats_human_choice"));
  assert.ok(!seen[1].prompt.includes(REPEATS_QUESTION));
});

test("screen reports the repeat and the human choice it is against", async () => {
  const s = await screen(withChoices, cfg, { screenJev: async () => ({ is_deviation: 0.89, repeats_human_choice: 0.9 }) });
  assert.deepEqual([s.isDeviation, s.repeatsHumanChoice, s.humanChoiceOf], [0.89, 0.9, 634]);
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

const taskRevState = { original: { title: "t", acceptance: ["a"] }, revised: { title: "t", acceptance: [] }, out_of_scope: [], design: [] };

test("a task revision holds at or above reject_at and accepts below it", async () => {
  fakeJev(rev(0.9, 0.1, 0.1));
  assert.equal((await judgeTaskRevision(taskRevState, cfg, "k")).verdict, "hold");
  fakeJev(rev(0.1, 0.1, 0.1));
  const j = await judgeTaskRevision(taskRevState, cfg, "k");
  assert.equal(j.verdict, "accept");
  assert.deepEqual(j.answers, rev(0.1, 0.1, 0.1));
  await assert.rejects(judgeTaskRevision(taskRevState, cfg, undefined), /key/);
  await assert.rejects(judgeTaskRevision(taskRevState, { ...cfg, model: "haiku" }, "k"), /haiku/);
});

const sat = (a: number, b: number) => ({ unmet_acceptance: a, different_task: b });
const satState = { task: { title: "502 on the health route" }, delivered: "diff of 4c9cb4a" };

test("a task closed as satisfied: #1632 (0.1, 0.1) accepts; unmet_acceptance 0.8 or different_task 0.9 holds", async () => {
  for (const [scores, want] of [[sat(0.1, 0.1), "accept"], [sat(0.8, 0.1), "hold"], [sat(0.1, 0.9), "hold"]] as const) {
    fakeJev(scores);
    const j = await judgeSatisfied(satState, cfg, "k");
    assert.equal(j.verdict, want);
    assert.deepEqual(j.answers, scores);
  }
});

test("the satisfied request goes to systemone with the two questions as nouls, through an injected ask too", async () => {
  const saved = process.env.TYPESAFE_URL;
  delete process.env.TYPESAFE_URL;
  try {
    const calls = fakeJev(sat(0.1, 0.1));
    await judgeSatisfied(satState, cfg, "secret");
    const { url, body, auth } = calls[0];
    assert.equal(url, "https://api.typesafe.ai/v1/systemone");
    assert.equal(auth, "Bearer secret");
    assert.deepEqual(body.state, satState);
    assert.deepEqual(Object.keys(body.questions).sort(), Object.keys(SATISFIED_QUESTIONS).sort());
    for (const [name, q] of Object.entries(body.questions) as [keyof typeof SATISFIED_QUESTIONS, any][]) {
      assert.deepEqual(q, { type: "noul", instructions: SATISFIED_QUESTIONS[name] });
    }
    let asked: unknown;
    const j = await judgeSatisfied(satState, cfg, "k", async (s) => { asked = s; return sat(0.2, 0.3); });
    assert.equal(asked, satState);
    assert.equal(j.verdict, "accept");
  } finally {
    if (saved !== undefined) process.env.TYPESAFE_URL = saved;
  }
});

test("a satisfied judgment with no key, a haiku model, an HTTP error or a missing answer throws", async () => {
  const calls = fakeJev(sat(0.1, 0.1));
  await assert.rejects(judgeSatisfied(satState, cfg, undefined), /key/);
  await assert.rejects(judgeSatisfied(satState, { ...cfg, model: "haiku" }, "k"), /haiku/);
  assert.equal(calls.length, 0);
  globalThis.fetch = (async () => new Response("no", { status: 500 })) as typeof fetch;
  await assert.rejects(judgeSatisfied(satState, cfg, "k"), /500/);
  fakeJev({ unmet_acceptance: 0.1 });
  await assert.rejects(judgeSatisfied(satState, cfg, "k"), /different_task/);
  fakeJev(sat(0.1, -1));
  await assert.rejects(judgeSatisfied(satState, cfg, "k"), /different_task/);
});

test("a rule change is judged by Jev on its three questions: hold at reject_at, else accept; never without a key", async () => {
  const state = { task: { title: "t" }, plan: "design", why: "the plan lets stats use links.application", gates: "architecture failed", rule_diff: "+allow" };
  let asked: { state: unknown; names: string[] } | undefined;
  const ok = await judgeRuleChange(state, cfg, "k", async (s, nouls) => {
    asked = { state: s, names: nouls.map((n) => n.name) };
    return { weakens_rule: 0.1, silences_failure: 0.2, code_is_wrong: 0.1 };
  });
  assert.equal(ok.verdict, "accept");
  assert.equal(asked?.state, state);
  assert.deepEqual(asked?.names, Object.keys(RULE_QUESTIONS));
  const held = await judgeRuleChange(state, cfg, "k", async () => ({ weakens_rule: 0.1, silences_failure: 0.9, code_is_wrong: 0.1 }));
  assert.equal(held.verdict, "hold");
  await assert.rejects(judgeRuleChange(state, cfg, undefined), /key/);
  await assert.rejects(judgeRuleChange(state, { ...cfg, model: "haiku" }, "k"), /haiku/);
  await assert.rejects(judgeRuleChange(state, cfg, "k", async () => ({ weakens_rule: 0.1 })), /silences_failure/);
});

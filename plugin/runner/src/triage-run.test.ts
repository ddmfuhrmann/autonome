import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import type { GateConfig, Triage } from "./context.js";
import { triagePending, triageRevisions } from "./triage-run.js";
import type { Answers } from "./triage.js";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });

const triage: Triage = { model: "jev-latest", fallback: "haiku", reject_at: 0.7, max_rounds: 2 };
const config = (t: Triage | null | undefined) => ({ triage: t }) as unknown as GateConfig["config"];
const answers = (o: Partial<Answers> = {}): Answers =>
  ({ drops_coverage: 0, contradicts: 0, workaround: 0, required_by_plan: 0, narrower: 0, rule_conflict: 0, ...o });

function fakeContext(items: unknown[]) {
  const calls: { url: string; body?: any }[] = [];
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return url.endsWith("/triage") && !init?.method ? json({ deviations: items, screen: [] }) : json({ status: "accepted" });
  };
  return calls;
}

test("triageRevisions: shape_changed skips Jev; a Jev failure is logged and the next plan is judged", async () => {
  process.env.TYPESAFE_API_KEY = "k";
  const calls: { url: string; body?: any }[] = [];
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return url.endsWith("/triage") ? json({ deviations: [], revisions: [
      { plan_id: 1, state: {}, shape_changed: true },
      { plan_id: 2, state: { n: 2 }, shape_changed: false },
      { plan_id: 3, state: { n: 3 }, shape_changed: false },
    ] }) : json({ status: "approved" });
  };
  const log: string[] = [];
  const ask = async (s: Record<string, unknown>) => {
    if (s.n === 2) throw new Error("jev: HTTP 500");
    return { drops_acceptance: 0, adds_out_of_scope: 0.88, changes_design: 0.1 };
  };
  try {
    const accepted = await triageRevisions("p", config(triage), { log: (l) => log.push(l), ask });
    assert.deepEqual(accepted, []);
  } finally {
    delete process.env.TYPESAFE_API_KEY;
  }
  const posts = calls.filter((c) => c.body);
  assert.equal(posts.length, 1);
  assert.match(posts[0].url, /\/api\/plans\/3\/rehearsal-triage$/);
  assert.equal(posts[0].body.verdict, "hold");
  assert.ok(log.includes("triage P1: shape changed, left for you"));
  assert.ok(log.includes("triage P2: left for you: jev: HTTP 500"));
  assert.ok(log.includes("triage P3: hold (jev, adds_out_of_scope 0.88)"));
});

test("with triage off or absent nothing is fetched", async () => {
  const calls = fakeContext([]);
  const log: string[] = [];
  await triagePending("p", config(undefined), { log: (l) => log.push(l) });
  await triagePending("p", config(null), { log: (l) => log.push(l) });
  await triagePending("p", config({ ...triage, model: "off" }), { log: (l) => log.push(l) });
  assert.equal(calls.length, 0);
});

test("a backend error on one item leaves it and judges the next", async () => {
  const calls = fakeContext([
    { decision_id: 1, round: 1, state: { deviation: { title: "bad" } } },
    { decision_id: 2, round: 1, state: { deviation: { title: "good" } } },
  ]);
  const log: string[] = [];
  const boom = async (s: Record<string, unknown>) => {
    if ((s.deviation as { title: string }).title === "bad") throw new Error("jev: HTTP 500");
    return answers();
  };
  const realError = console.error;
  console.error = () => undefined;
  try {
    await triagePending("p", config({ ...triage, fallback: "off" }), { log: (l) => log.push(l), backends: { jev: boom } });
  } finally {
    console.error = realError;
  }
  const posts = calls.filter((c) => c.body);
  assert.equal(posts.length, 1);
  assert.match(posts[0].url, /\/api\/decisions\/2\/triage$/);
  assert.ok(log.some((l) => /D1: left proposed/.test(l)));
  assert.ok(log.some((l) => l === "triage D2: accept (jev, drops_coverage 0)"));
});

test("the verdict POST carries backend, answers, state and round", async () => {
  const calls = fakeContext([{ decision_id: 9, round: 2, state: { deviation: { title: "t" } } }]);
  const log: string[] = [];
  await triagePending("p", config(triage), {
    log: (l) => log.push(l),
    backends: { jev: async () => answers({ contradicts: 0.91 }) },
  });
  const post = calls.find((c) => c.body)!;
  assert.match(post.url, /\/api\/decisions\/9\/triage$/);
  assert.deepEqual(post.body, {
    backend: "jev", model: "jev-latest", state: JSON.stringify({ deviation: { title: "t" } }),
    answers: { drops_coverage: "0", contradicts: "0.91", workaround: "0", required_by_plan: "0", narrower: "0", rule_conflict: "0" },
    verdict: "reject", round: 2,
  });
  assert.deepEqual(log, ["triage D9: reject (jev, contradicts 0.91)"]);
});

test("a rule conflict is posted as escalate and logged with the conflict", async () => {
  const calls = fakeContext([{ decision_id: 7, round: 1, state: { deviation: { title: "t" } } }]);
  const log: string[] = [];
  await triagePending("p", config(triage), {
    log: (l) => log.push(l),
    backends: { jev: async () => answers({ rule_conflict: 0.91, contradicts: 0.86, drops_coverage: 0.17 }) },
  });
  const post = calls.find((c) => c.body)!;
  assert.match(post.url, /\/api\/decisions\/7\/triage$/);
  assert.equal(post.body.verdict, "escalate");
  assert.equal(post.body.answers.rule_conflict, "0.91");
  assert.equal(post.body.answers.contradicts, "0.86");
  assert.equal(post.body.round, 1);
  assert.equal(log.length, 1);
  assert.equal(log[0], "triage D7: escalate (jev, rule_conflict 0.91): a rule conflict for you");
});

/** A context whose first GET lists `screen` and whose later GETs list `after` deviations. */
function screeningContext(screenItems: unknown[], after: unknown[]) {
  const calls: { url: string; body?: any }[] = [];
  let gets = 0;
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.endsWith("/triage") && !init?.method) {
      return json(gets++ === 0 ? { deviations: [], screen: screenItems } : { deviations: after, screen: [] });
    }
    if (url.endsWith("/screen")) return json({ status: "proposed", deviation: calls.at(-1)!.body.is_deviation >= 0.7 });
    return json({ status: "accepted" });
  };
  return calls;
}

// #594: a test weakened (hash instead of immutability) to avoid a suppression, recorded as within plan
const weakened = { decision: { title: "assert the hash, not immutability", deviation: false },
  task: { title: "freeze the config" } };

test("screening reopens a #594-shaped decision and judges it in the same run", async () => {
  const calls = screeningContext(
    [{ decision_id: 594, state: weakened }, { decision_id: 595, state: { decision: { title: "fine" } } }],
    [{ decision_id: 594, round: 0, state: weakened }]);
  const log: string[] = [];
  const events: { text: string }[] = [];
  await triagePending("p", config(triage), {
    log: (l) => log.push(l),
    events: { push: (e: { text: string }) => events.push(e) } as never,
    backends: {
      screenJev: async (s) => (JSON.stringify(s).includes("hash") ? 0.9 : 0.1),
      jev: async () => answers({ drops_coverage: 0.95 }),
    },
  });
  const screens = calls.filter((c) => c.url.endsWith("/screen"));
  assert.deepEqual(screens.map((c) => c.url.replace(/.*decisions\//, "")), ["594/screen", "595/screen"]);
  assert.deepEqual(screens[0].body, { backend: "jev", model: "jev-latest", is_deviation: 0.9, reject_at: 0.7 });
  assert.deepEqual(log, [
    "triage screen D594: 0.9 (jev) → deviation",
    "triage screen D595: 0.1 (jev)",
    "triage D594: reject (jev, drops_coverage 0.95)",
  ]);
  assert.equal(events.length, 3);
  const verdictPost = calls.find((c) => c.url.endsWith("/594/triage"))!;
  assert.equal(verdictPost.body.verdict, "reject");
});

test("a screening error leaves the decision unscreened and still judges the deviations", async () => {
  const calls = screeningContext([{ decision_id: 7, state: { decision: {} } }], []);
  const log: string[] = [];
  const realError = console.error;
  console.error = () => undefined;
  try {
    await triagePending("p", config({ ...triage, fallback: "off" }), {
      log: (l) => log.push(l),
      backends: { screenJev: async () => { throw new Error("jev: HTTP 500"); } },
    });
  } finally {
    console.error = realError;
  }
  assert.ok(log.some((l) => /screen D7: left unscreened/.test(l)));
  assert.equal(calls.filter((c) => c.url.endsWith("/screen")).length, 0);
});

test("a screening error does not stop the deviations already waiting", async () => {
  const calls: { url: string; body?: any }[] = [];
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return url.endsWith("/triage") && !init?.method
      ? json({ deviations: [{ decision_id: 2, round: 0, state: { deviation: {} } }],
               screen: [{ decision_id: 1, state: {} }] })
      : json({ status: "accepted" });
  };
  const realError = console.error;
  console.error = () => undefined;
  try {
    await triagePending("p", config({ ...triage, fallback: "off" }), {
      log: () => undefined,
      backends: { screenJev: async () => { throw new Error("down"); }, jev: async () => answers() },
    });
  } finally {
    console.error = realError;
  }
  assert.ok(calls.some((c) => c.url.endsWith("/decisions/2/triage") && c.body));
  assert.ok(!calls.some((c) => c.url.endsWith("/screen")));
});

test("no key and no jev backend: the fallback judges", async () => {
  const saved = process.env.TYPESAFE_API_KEY;
  const home = process.env.AUTONOME_HOME;
  delete process.env.TYPESAFE_API_KEY;
  process.env.AUTONOME_HOME = "/nonexistent-autonome-home";
  const calls = fakeContext([{ decision_id: 3, round: 0, state: { deviation: { title: "t" } } }]);
  const realError = console.error;
  console.error = () => undefined;
  try {
    await triagePending("p", config(triage), { log: () => undefined, backends: { haiku: async () => answers() } });
  } finally {
    console.error = realError;
    if (saved !== undefined) process.env.TYPESAFE_API_KEY = saved;
    if (home === undefined) delete process.env.AUTONOME_HOME; else process.env.AUTONOME_HOME = home;
  }
  assert.equal(calls.find((c) => c.body)?.body.backend, "haiku");
});

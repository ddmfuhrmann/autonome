import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import type { GateConfig, Triage } from "./context.js";
import { triagePending } from "./triage-run.js";
import type { Answers } from "./triage.js";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });

const triage: Triage = { model: "jev-latest", fallback: "haiku", reject_at: 0.7, max_rounds: 2 };
const config = (t: Triage | null | undefined) => ({ triage: t }) as unknown as GateConfig["config"];
const answers = (o: Partial<Answers> = {}): Answers =>
  ({ drops_coverage: 0, contradicts: 0, workaround: 0, required_by_plan: 0, narrower: 0, ...o });

function fakeContext(items: unknown[]) {
  const calls: { url: string; body?: any }[] = [];
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return url.endsWith("/triage") && !init?.method ? json(items) : json({ status: "accepted" });
  };
  return calls;
}

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
    { decision_id: 1, round: 0, state: { deviation: { title: "bad" } } },
    { decision_id: 2, round: 0, state: { deviation: { title: "good" } } },
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
    answers: { drops_coverage: "0", contradicts: "0.91", workaround: "0", required_by_plan: "0", narrower: "0" },
    verdict: "reject", round: 2,
  });
  assert.deepEqual(log, ["triage D9: reject (jev, contradicts 0.91)"]);
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

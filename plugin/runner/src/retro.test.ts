import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import type { Plan } from "./context.js";
import { retroFinishedWaves } from "./retro.js";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
const wave = { id: 18, project: "p", status: "done", feature: null,
  body: { title: "MVP", tasks: [], features: [{ plan_id: 15 }, { plan_id: 16 }] } } as unknown as Plan;

function context(retroAfterWave: boolean | undefined, marked: unknown[]) {
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/gate-config")) return json({ config: { gates: [], retro_after_wave: retroAfterWave } });
    if (url.endsWith("/waves/without-retro")) return json([wave]);
    if (url.endsWith("/waves/18/retro-done")) { marked.push(JSON.parse(String(init?.body))); return json({ ok: true }); }
    return json({});
  };
}

test("a finished wave gets one retro and is marked with what the retro proposed", async () => {
  const marked: unknown[] = [];
  context(undefined, marked);
  const seen: number[] = [];
  const n = await retroFinishedWaves({ project: "p", repo: ".", log: () => {} },
    async (w) => { seen.push(w.id); return "proposed C-91 input validation (evidence T-25, T-27)"; });
  assert.equal(n, 1);
  assert.deepEqual(seen, [18]);
  assert.deepEqual(marked, [{ summary: "proposed C-91 input validation (evidence T-25, T-27)" }]);
});

test("a wave is not marked when the account ran out of usage", async () => {
  const marked: unknown[] = [];
  context(undefined, marked);
  await retroFinishedWaves({ project: "p", repo: ".", log: () => {} }, async () => { throw new Error("You've hit your session limit · resets 3:40pm"); });
  assert.deepEqual(marked, []);
});

test("a failed retro session still marks the wave, and the gate config can turn the retro off", async () => {
  const marked: { summary: string }[] = [];
  context(undefined, marked);
  await retroFinishedWaves({ project: "p", repo: ".", log: () => {} }, async () => { throw new Error("rate limited"); });
  assert.match(marked[0].summary, /the retro session failed: Error: rate limited/);
  const none: unknown[] = [];
  context(false, none);
  assert.equal(await retroFinishedWaves({ project: "p", repo: ".", log: () => {} }, async () => "x"), 0);
  assert.deepEqual(none, []);
});

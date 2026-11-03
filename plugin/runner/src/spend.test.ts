import { test } from "node:test";
import assert from "node:assert/strict";
import { context } from "./context.js";

test("a session's cost is posted by role, skipped when zero, and a failing post never throws", async () => {
  const real = globalThis.fetch;
  const posted: unknown[] = [];
  try {
    globalThis.fetch = (async (_url: string, init?: RequestInit) => {
      posted.push(JSON.parse(String(init?.body)));
      return new Response(JSON.stringify({ recorded: 1 }), { status: 201, headers: { "content-type": "application/json" } });
    }) as typeof fetch;
    await context.spend("p", "preflight", 0.18, { plan_id: 3, feature: "links" });
    await context.spend("p", "tester", 0);
    assert.deepEqual(posted, [{ project: "p", role: "preflight", cost_usd: 0.18, plan_id: 3, feature: "links" }]);

    globalThis.fetch = (async () => new Response(JSON.stringify({ error: "no" }), {
      status: 400, headers: { "content-type": "application/json" } })) as typeof fetch;
    await context.spend("p", "retro", 0.5); // resolves: the record is best effort
  } finally {
    globalThis.fetch = real;
  }
});

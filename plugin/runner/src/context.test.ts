import assert from "node:assert/strict";
import { mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import { conduct } from "./conduct.js";
import { ContextError, call } from "./context.js";
import { git } from "./git.js";

process.env.AUTONOME_CONTEXT_RETRY_MS = "1";
const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

const html500 = () => new Response("Internal Server Error\n<html>…</html>", { status: 500, headers: { "content-type": "text/plain" } });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

test("a 500 with an HTML body is retried, then reported as a ContextError naming the call", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; return html500(); };
  await assert.rejects(call("/api/plans/16/rehearsal", { method: "POST", body: "{}" }), (e: Error) =>
    e instanceof ContextError && e.message === "context API 500 on POST /api/plans/16/rehearsal: Internal Server Error");
  assert.equal(calls, 3);
});

test("a transient 500 recovers on retry, and a 4xx is an answer, not an outage", async () => {
  const replies = [html500(), json({ ok: 1 })];
  globalThis.fetch = async () => replies.shift()!;
  assert.deepEqual(await call("/x"), { ok: 1 });
  let calls = 0;
  globalThis.fetch = async () => { calls++; return json({ error: "plan 9 is not approved" }, 409); };
  await assert.rejects(call("/y", { method: "POST" }), (e: Error) => !(e instanceof ContextError) && /409 plan 9/.test(e.message));
  assert.equal(calls, 1);
});

test("an infrastructure error hands the task back to pending and stops the conductor, never blocks it", async () => {
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-ctx-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  writeFileSync(join(repo, "a.txt"), "a\n");
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "init");
  const transitions: string[] = [];
  const task = { id: 25, project: "p", status: "playing", feature: "links", layers: [],
    body: { title: "Links", spec: "s", acceptance: [], plan_id: 16, seq: 1, fermata: false } };
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/tasks/claim")) return json({ task, rehearse: null, soundcheck: null });
    if (url.endsWith("/gate-config")) return html500();
    if (url.includes("/transition")) { transitions.push(JSON.parse(String(init?.body)).to); return json({ ok: true }); }
    return json({});
  };
  await assert.rejects(conduct({ project: "p", repo, maxTasks: 5, push: false, log: () => {} }),
    (e: Error) => e instanceof ContextError && /500 on GET \/api\/projects\/p\/gate-config/.test(e.message));
  assert.deepEqual(transitions, ["pending"]);
});

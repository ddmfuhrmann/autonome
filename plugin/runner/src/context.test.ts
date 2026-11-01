import assert from "node:assert/strict";
import { mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import { conduct } from "./conduct.js";
import { ContextError, call, languageRule, type GateConfig } from "./context.js";
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
  let gateConfigCalls = 0; // the first read is the doctor's; the conductor's read then fails
  const task = { id: 25, project: "p", status: "playing", feature: "links", layers: [],
    body: { title: "Links", spec: "s", acceptance: [], plan_id: 16, seq: 1, fermata: false } };
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/tasks/claim")) return json({ task, rehearse: null, soundcheck: null });
    if (url.endsWith("/gate-config")) return ++gateConfigCalls === 1 ? json({ config: { gates: [{ name: "t", run: "true" }] } }) : html500();
    if (url.includes("/transition")) { transitions.push(JSON.parse(String(init?.body)).to); return json({ ok: true }); }
    return json({});
  };
  await assert.rejects(conduct({ project: "p", repo, maxTasks: 5, push: false, log: () => {} }),
    (e: Error) => e instanceof ContextError && /500 on GET \/api\/projects\/p\/gate-config/.test(e.message));
  assert.deepEqual(transitions, ["pending"]);
});

/** Runs the real conduct loop against a fake context; once the task is claimed the gate config read fails as `afterClaim` says. */
async function conductWithFailure(runs: { outcome: string }[], afterClaim: () => Response) {
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-err-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  writeFileSync(join(repo, "a.txt"), "a\n");
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "init");
  const transitions: { to: string; run?: { outcome: string; reason?: string } }[] = [];
  let claimed = false;
  let failed = false;
  const task = { id: 31, project: "p", status: "playing", feature: "links", layers: [],
    body: { title: "Links", spec: "s", acceptance: [], plan_id: 16, seq: 1, fermata: false, runs } };
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/tasks/claim")) {
      const first = !claimed;
      claimed = true;
      // The doctor has passed. conduct ends by spawning autonome-docs; keep that child off a real context on the default port.
      process.env.AUTONOME_CONTEXT_URL = "http://127.0.0.1:9";
      process.env.AUTONOME_CONTEXT_WAIT_S = "0";
      return json({ task: first ? task : null, rehearse: null, soundcheck: null });
    }
    if (url.endsWith("/gate-config")) {
      if (claimed && !failed) { failed = true; return afterClaim(); } // only the task's own read fails; the idle tune after it is fine
      return json({ config: { gates: [{ name: "t", run: "true" }] } });
    }
    if (url.includes("/transition")) { transitions.push(JSON.parse(String(init?.body))); return json({ ok: true }); }
    if (url.endsWith("/waves/untuned")) return json([]);
    return json({});
  };
  const url = process.env.AUTONOME_CONTEXT_URL;
  try {
    const error = await conduct({ project: "p", repo, maxTasks: 5, push: false, log: () => {} }).then(() => undefined, (e: Error) => e);
    return { transitions, error };
  } finally {
    if (url === undefined) delete process.env.AUTONOME_CONTEXT_URL; else process.env.AUTONOME_CONTEXT_URL = url;
    delete process.env.AUTONOME_CONTEXT_WAIT_S;
  }
}

test("a runner exception in the loop hands the task back to pending with an error run", async () => {
  const { transitions, error } = await conductWithFailure([], () => json({ config: null })); // a context older than the runner
  assert.equal(error, undefined);
  assert.equal(transitions.length, 1);
  assert.equal(transitions[0].to, "pending");
  assert.equal(transitions[0].run?.outcome, "error");
  assert.match(transitions[0].run!.reason!, /TypeError/);
});

test("the third runner exception in a row blocks the task in the loop", async () => {
  const { transitions } = await conductWithFailure([{ outcome: "error" }, { outcome: "error" }], () => json({ config: null }));
  assert.equal(transitions.length, 1);
  assert.equal(transitions[0].to, "blocked");
  assert.match(transitions[0].run!.reason!, /^runner error \(3 in a row\): TypeError/);
});

test("the monthly spend limit hands the task back to pending without an error run", async () => {
  const limit = "You've hit your monthly spend limit · raise it at https://claude.ai/settings/usage";
  const { transitions, error } = await conductWithFailure([{ outcome: "error" }, { outcome: "error" }], () => json({ error: limit }, 409));
  assert.match(error!.message, /spend limit/);
  assert.deepEqual(transitions, [{ to: "pending" }]); // no error run, so the streak does not grow toward blocked
});

test("the language rule names the configured language and is empty without one", () => {
  const cfg = { language: "pt-BR" } as GateConfig["config"];
  assert.match(languageRule(cfg), /in pt-BR\. Keep code, identifiers/);
  assert.equal(languageRule({} as GateConfig["config"]), "");
});

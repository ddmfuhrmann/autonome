import assert from "node:assert/strict";
import { mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import { ContextError, call, context, languageRule, roleModel, type GateConfig } from "./context.js";
import { run } from "./run.js";
import { git } from "./git.js";

process.env.AUTONOME_CONTEXT_RETRY_MS = "1";
const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

const html500 = () => new Response("Internal Server Error\n<html>…</html>", { status: 500, headers: { "content-type": "text/plain" } });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

test("a 500 with an HTML body is retried, then reported as a ContextError naming the call", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; return html500(); };
  await assert.rejects(call("/api/plans/16/preflight", { method: "POST", body: "{}" }), (e: Error) =>
    e instanceof ContextError && e.message === "context API 500 on POST /api/plans/16/preflight: Internal Server Error");
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

test("an infrastructure error hands the task back to pending and stops the runner, never blocks it", async () => {
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-ctx-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  writeFileSync(join(repo, "a.txt"), "a\n");
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "init");
  const transitions: string[] = [];
  let gateConfigCalls = 0; // the first read is the doctor's; the runner's read then fails
  const task = { id: 25, project: "p", status: "running", feature: "links", layers: [],
    body: { title: "Links", spec: "s", acceptance: [], plan_id: 16, seq: 1, hold_before_merge: false } };
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/tasks/claim")) return json({ task, preflight: null, smoke_test: null });
    if (url.endsWith("/gate-config")) return ++gateConfigCalls === 1 ? json({ config: { gates: [{ name: "t", run: "true" }] } }) : html500();
    if (url.includes("/transition")) { transitions.push(JSON.parse(String(init?.body)).to); return json({ ok: true }); }
    return json({});
  };
  await assert.rejects(run({ project: "p", repo, maxTasks: 5, push: false, log: () => {} }),
    (e: Error) => e instanceof ContextError && /500 on GET \/api\/projects\/p\/gate-config/.test(e.message));
  assert.deepEqual(transitions, ["pending"]);
});

/** Runs the real run loop against a fake context; once the task is claimed the gate config read fails as `afterClaim` says. */
async function runWithFailure(runs: { outcome: string }[], afterClaim: () => Response) {
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
  const task = { id: 31, project: "p", status: "running", feature: "links", layers: [],
    body: { title: "Links", spec: "s", acceptance: [], plan_id: 16, seq: 1, hold_before_merge: false, runs } };
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/tasks/claim")) {
      const first = !claimed;
      claimed = true;
      // The doctor has passed. The run ends by spawning autonome-docs; keep that child off a real context on the default port.
      process.env.AUTONOME_CONTEXT_URL = "http://127.0.0.1:9";
      process.env.AUTONOME_CONTEXT_WAIT_S = "0";
      return json({ task: first ? task : null, preflight: null, smoke_test: null });
    }
    if (url.endsWith("/gate-config")) {
      if (claimed && !failed) { failed = true; return afterClaim(); } // only the task's own read fails; the idle retro after it is fine
      return json({ config: { gates: [{ name: "t", run: "true" }] } });
    }
    if (url.includes("/transition")) { transitions.push(JSON.parse(String(init?.body))); return json({ ok: true }); }
    if (url.endsWith("/waves/without-retro")) return json([]);
    return json({});
  };
  const url = process.env.AUTONOME_CONTEXT_URL;
  try {
    const error = await run({ project: "p", repo, maxTasks: 5, push: false, log: () => {} }).then(() => undefined, (e: Error) => e);
    return { transitions, error };
  } finally {
    if (url === undefined) delete process.env.AUTONOME_CONTEXT_URL; else process.env.AUTONOME_CONTEXT_URL = url;
    delete process.env.AUTONOME_CONTEXT_WAIT_S;
  }
}

test("a runner exception in the loop hands the task back to pending with an error run", async () => {
  const { transitions, error } = await runWithFailure([], () => json({ config: null })); // a context older than the runner
  assert.equal(error, undefined);
  assert.equal(transitions.length, 1);
  assert.equal(transitions[0].to, "pending");
  assert.equal(transitions[0].run?.outcome, "error");
  assert.match(transitions[0].run!.reason!, /TypeError/);
});

test("the third runner exception in a row blocks the task in the loop", async () => {
  const { transitions } = await runWithFailure([{ outcome: "error" }, { outcome: "error" }], () => json({ config: null }));
  assert.equal(transitions.length, 1);
  assert.equal(transitions[0].to, "blocked");
  assert.match(transitions[0].run!.reason!, /^runner error \(3 in a row\): TypeError/);
});

test("the monthly spend limit hands the task back to pending without an error run", async () => {
  const limit = "You've hit your monthly spend limit · raise it at https://claude.ai/settings/usage";
  const { transitions, error } = await runWithFailure([{ outcome: "error" }, { outcome: "error" }], () => json({ error: limit }, 409));
  assert.match(error!.message, /spend limit/);
  assert.deepEqual(transitions, [{ to: "pending" }]); // no error run, so the streak does not grow toward blocked
});

test("the loop: preflight holds the plan, Jev accepts, and the revised task is claimed and run in the same run", async () => {
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-rev-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  writeFileSync(join(repo, "a.txt"), "a\n");
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "init");
  const mk = (id: number) => ({ id, project: "p", status: "running", feature: "links", layers: [],
    body: { title: `T${id}`, spec: "s", acceptance: [], plan_id: 677, seq: 1, hold_before_merge: false } });
  const plan = { id: 677, body: { title: "shortlink-v6" } };
  const claims = [{ task: mk(4), preflight: plan }, { task: mk(5), preflight: null }]; // 5 stands for the revised task
  const events: string[] = [];
  let held = false;
  process.env.TYPESAFE_API_KEY = "k";
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/tasks/claim")) {
      // The doctor has passed; keep the closing autonome-docs child off a real context on the default port.
      process.env.AUTONOME_CONTEXT_URL = "http://127.0.0.1:9";
      process.env.AUTONOME_CONTEXT_WAIT_S = "0";
      const next = claims.shift();
      events.push(`claim ${next?.task.id ?? "none"}`);
      return json({ task: next?.task ?? null, preflight: next?.preflight ?? null, smoke_test: null });
    }
    if (url.endsWith("/gate-config")) {
      return json({ config: { gates: [{ name: "t", run: "true" }],
        triage: { model: "jev-latest", fallback: "off", reject_at: 0.7, max_rounds: 2 } } });
    }
    if (url.endsWith("/api/plans/677/preflight")) { held = true; return json({ plan_status: "held" }); }
    if (url.endsWith("/triage")) {
      return json({ deviations: [], screen: [], revisions: held
        ? [{ plan_id: 677, state: { original: [], revised: [] }, shape_changed: false }] : [] });
    }
    if (url.endsWith("/api/plans/677/preflight-triage")) {
      held = false;
      events.push(`post ${JSON.parse(String(init?.body)).verdict}`);
      return json({ status: "approved" });
    }
    if (url.includes("/transition")) { events.push(`${url.match(/tasks\/(\d+)/)![1]} -> ${JSON.parse(String(init?.body)).to}`); return json({ ok: true }); }
    if (url.endsWith("/waves/without-retro")) return json([]);
    return json({});
  };
  const url = process.env.AUTONOME_CONTEXT_URL;
  try {
    const played = await run({ project: "p", repo, maxTasks: 5, push: false, log: () => {}, seams: {
      preflight: async () => ({ verdict: "revise" }) as never,
      ask: async () => ({ drops_acceptance: 0, adds_out_of_scope: 0, changes_design: 0.13 }),
      runTask: async (t) => { events.push(`run ${t.id}`); return { at: "", branch: "", worktree: "", base: "", outcome: "merged" }; },
    } });
    assert.equal(played, 1);
  } finally {
    if (url === undefined) delete process.env.AUTONOME_CONTEXT_URL; else process.env.AUTONOME_CONTEXT_URL = url;
    delete process.env.AUTONOME_CONTEXT_WAIT_S;
    delete process.env.TYPESAFE_API_KEY;
  }
  assert.deepEqual(events, ["claim 4", "4 -> pending", "post accept", "claim 5", "run 5", "claim none"]);
});

// The contract with the context: it moves a claimed task to `running`, the runner reads the gate config's
// new keys, and every call it makes goes to the new endpoints with the new body fields.
test("the claim returns a running task, the new gate config keys are read, and the new endpoints are called", async () => {
  const calls: { method: string; url: string; body?: any }[] = [];
  const config = { gates: [], retries: { max: 4, stop_on_repeat: false }, hold_before_merge: { features: ["billing"] },
    review_after_merge: { features: ["links"] }, retro_after_wave: false, smoke_test: null,
    models: { implementer: { model: "opus", effort: "high" } } };
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    calls.push({ method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.endsWith("/tasks/claim")) {
      return json({ task: { id: 9, project: "p", status: "running", feature: "links", layers: [],
        body: { title: "t", spec: "s", acceptance: [], plan_id: 1, seq: 1, hold_before_merge: false, after_merge: true } },
        preflight: null, smoke_test: null });
    }
    if (url.endsWith("/gate-config")) return json({ version: 1, config });
    return json({ ok: true, status: "pending", plan_status: "approved", outcome: "done" });
  };
  const claimed = await context.claim("p");
  assert.equal(claimed.task?.status, "running");
  assert.equal(claimed.task?.body.after_merge, true);
  assert.equal(claimed.preflight, null);
  assert.equal(claimed.smoke_test, null);
  const gc = (await context.gateConfig("p")).config;
  assert.equal(gc.retries.max, 4);
  assert.deepEqual(gc.hold_before_merge?.features, ["billing"]);
  assert.deepEqual(gc.review_after_merge?.features, ["links"]);
  assert.equal(gc.retro_after_wave, false);
  assert.deepEqual(roleModel(gc, "implementer"), { model: "opus", effort: "high" });

  await context.transition(9, "pending");
  await context.preflight(1, { verdict: "ready", notes: "" });
  await context.smokeTest(1, { passed: true, report: "ok", checks: [], fixes: [] });
  await context.wavesWithoutRetro("p");
  await context.waveRetroDone(3, "s");
  const paths = calls.map((c) => `${c.method} ${new URL(c.url).pathname}`);
  assert.deepEqual(paths.slice(2), [
    "POST /api/tasks/9/transition", "POST /api/plans/1/preflight", "POST /api/plans/1/smoke-test",
    "GET /api/projects/p/waves/without-retro", "POST /api/waves/3/retro-done"]);
  assert.deepEqual(calls[2].body, { to: "pending" });
});

test("the language rule names the configured language and is empty without one", () => {
  const cfg = { language: "pt-BR" } as GateConfig["config"];
  assert.match(languageRule(cfg), /in pt-BR\. Keep code, identifiers/);
  assert.equal(languageRule({} as GateConfig["config"]), "");
});

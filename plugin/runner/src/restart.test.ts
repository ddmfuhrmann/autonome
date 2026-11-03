// The context restarting under the runner (shortlink, 2026-10-31): it waits, and a merge it could
// not report is recorded by the next run.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readdirSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import { fileURLToPath } from "node:url";

const PLUGIN_BIN = join(fileURLToPath(import.meta.url), "..", "..", "..", "bin");

const home = mkdtempSync(join(tmpdir(), "autonome-home-"));
process.env.AUTONOME_HOME = home;
process.env.AUTONOME_CONTEXT_RETRY_MS = "1";
process.env.AUTONOME_CONTEXT_WAIT_S = "2";
const { call } = await import("./context.js");
const { run } = await import("./run.js");
const { git } = await import("./git.js");

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });

test("an unreachable context is waited for, and the same call goes through once it is back", async () => {
  let up = false;
  let healthChecks = 0;
  globalThis.fetch = async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/health")) {
      if (++healthChecks === 3) up = true;
      if (!up) throw new TypeError("fetch failed");
      return json({ status: "ok" });
    }
    if (!up) throw new TypeError("fetch failed");
    return json({ task: null });
  };
  assert.deepEqual(await call("/api/projects/p/tasks/claim", { method: "POST" }), { task: null });
  assert.equal(healthChecks, 3);
});

test("a merge the context could not record is recorded as done by the next run", async () => {
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-repo-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init");
  mkdirSync(join(home, "state"), { recursive: true });
  writeFileSync(join(home, "state", "merged-p-25.json"), JSON.stringify({ task_id: 25, run: { outcome: "merged" } }));
  const transitions: unknown[] = [];
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text" }] });
    if (url.endsWith("/waves/without-retro")) return json([]);
    if (url.endsWith("/gate-config")) return json({ config: { gates: [{ name: "t", run: "true" }] } });
    if (url.includes("/tasks/25/transition")) { transitions.push(JSON.parse(String(init?.body))); return json({ status: "done" }); }
    return json({ task: null, preflight: null, smoke_test: null });
  };
  await run({ project: "p", repo, maxTasks: 1, push: false, log: () => {} });
  assert.deepEqual(transitions, [{ to: "done", run: { outcome: "merged" } }]);
  assert.deepEqual(readdirSync(join(home, "state")).filter((f) => f.startsWith("merged-")), []);
});

async function runWithStatus(status: number, key?: string): Promise<{ posted: string[]; logs: string[] }> {
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-repo-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init");
  const posted: string[] = [];
  const logs: string[] = [];
  const saved = process.env.TYPESAFE_API_KEY;
  if (key) process.env.TYPESAFE_API_KEY = key; else delete process.env.TYPESAFE_API_KEY;
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text" }] });
    if (url.endsWith("/waves/without-retro")) return json([]);
    if (url.endsWith("/gate-config")) return json({ config: { gates: [{ name: "t", run: "true" }] } });
    if (url.endsWith("/runner-status")) {
      posted.push(String(init?.body));
      return new Response(JSON.stringify({ error: "boom" }), { status, headers: { "content-type": "application/json" } });
    }
    return json({ task: null, preflight: null, smoke_test: null });
  };
  try {
    await run({ project: "p", repo, maxTasks: 1, push: false, log: (l: string) => logs.push(l) });
  } finally {
    if (saved === undefined) delete process.env.TYPESAFE_API_KEY; else process.env.TYPESAFE_API_KEY = saved;
  }
  return { posted, logs };
}

test("a context that answers 500 to the runner status does not stop the run, which logs a warning", async () => {
  const { posted, logs } = await runWithStatus(500);
  assert.ok(posted.length >= 1);
  assert.ok(logs.some((l) => l.startsWith("warning:") && l.includes("runner-status")), logs.join("\n"));
});

test("the runner status body says the key was found and never carries its value", async () => {
  const { posted } = await runWithStatus(200, "sk-test-123");
  assert.deepEqual(posted.map((b) => JSON.parse(b)), [{ triage_key: true }]);
  assert.ok(posted.every((b) => !b.includes("sk-test-123")));
});

test("the runner status body says the key was not found when there is none", async () => {
  const { posted } = await runWithStatus(200);
  assert.deepEqual(posted.map((b) => JSON.parse(b)), [{ triage_key: false }]);
});

test("a setup task on a greenfield project may run the pending gates' tools, and no gate blocks it", async () => {
  const { implementerTools } = await import("./run.js");
  const { execFile } = await import("node:child_process");
  const { promisify } = await import("node:util");
  const { createServer } = await import("node:http");
  const gc = { version: null, config: { gates: [], allowed_commands: [], retries: { max: 3, stop_on_repeat: true },
    budget: { task_tokens: 1, task_minutes: 1, plan_tokens: 1 }, review: { enabled: true, max_rounds: 2 }, smoke_test: null },
    pending: [{ name: "mypy", run: "uv run mypy", task_id: 202, plan_id: 185 }] };
  assert.ok(implementerTools(gc).includes("Bash(uv:*)"));

  // The judge's gate run on a config with no gates yet: nothing applies, so it passes.
  const server = createServer((_req, res) => { res.setHeader("content-type", "application/json"); res.end(JSON.stringify(gc)); });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  const port = (server.address() as { port: number }).port;
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-greenfield-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init");
  writeFileSync(join(repo, "pyproject.toml"), "[project]\nname = 'x'\n");
  // Async: the fake context lives in this process and must keep answering while the gates run.
  const r = await promisify(execFile)(join(PLUGIN_BIN, "autonome-gates"), ["--json", "--base", "HEAD"], { cwd: repo,
    env: { ...process.env, AUTONOME_CONTEXT_URL: `http://127.0.0.1:${port}`, AUTONOME_PROJECT: "shortlink-4" } });
  server.close();
  assert.deepEqual(JSON.parse(r.stdout), { passed: true, config_version: null, outcomes: [], skipped: [] });
});

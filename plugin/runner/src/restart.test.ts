// The context restarting under the maestro (shortlink, 2026-10-31): it waits, and a merge it could
// not report is recorded by the next conduct.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readdirSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

const home = mkdtempSync(join(tmpdir(), "autonome-home-"));
process.env.AUTONOME_HOME = home;
process.env.AUTONOME_CONTEXT_RETRY_MS = "1";
process.env.AUTONOME_CONTEXT_WAIT_S = "2";
const { call } = await import("./context.js");
const { conduct } = await import("./conduct.js");
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

test("a merge the context could not record is recorded as done by the next conduct", async () => {
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-repo-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init");
  mkdirSync(join(home, "state"), { recursive: true });
  writeFileSync(join(home, "state", "merged-p-25.json"), JSON.stringify({ task_id: 25, run: { outcome: "merged" } }));
  const transitions: unknown[] = [];
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.includes("/tasks/25/transition")) { transitions.push(JSON.parse(String(init?.body))); return json({ status: "done" }); }
    return json({ task: null, rehearse: null, soundcheck: null });
  };
  await conduct({ project: "p", repo, maxTasks: 1, push: false, log: () => {} });
  assert.deepEqual(transitions, [{ to: "done", run: { outcome: "merged" } }]);
  assert.deepEqual(readdirSync(join(home, "state")).filter((f) => f.startsWith("merged-")), []);
});

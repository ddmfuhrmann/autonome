import assert from "node:assert/strict";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import type { Options } from "@anthropic-ai/claude-agent-sdk";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import type { GateConfig, Task } from "./context.js";
import { implement, type RunOptions } from "./run.js";
import { smokeTest } from "./smoke-test.js";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

const alive = (marker: string) => spawnSync("pgrep", ["-f", marker]).status === 0;
const unique = (n: number) => `sleep ${n}${Math.floor(Math.random() * 900 + 100)}`;

test("a smoke test check whose grandchild sleeps is killed whole at its ceiling", async () => {
  const marker = unique(3);
  const dir = mkdtempSync(join(tmpdir(), "smoke-proc-"));
  const started = Date.now();
  const r = await smokeTest(
    { id: 1, project: "p", status: "approved", feature: "f", body: { title: "t", tasks: [] } },
    { url: "http://127.0.0.1:1", seed: null, tester: false, max_fixes: 1, up: "true", down: "true",
      checks: [{ name: "slow", run: `sh -c '${marker} & wait'`, timeout_s: 1 }] },
    dir, undefined, () => {});
  assert.ok(Date.now() - started < 10_000);
  assert.equal(r.checks[0].passed, false);
  assert.match(r.checks[0].output, /timed out after 1s \(killed the process group\)/);
  assert.equal(alive(marker), false);
});

test("a smoke test check that ignores SIGTERM is killed after the grace period", async () => {
  const marker = unique(7);
  const dir = mkdtempSync(join(tmpdir(), "smoke-proc-"));
  const started = Date.now();
  const r = await smokeTest(
    { id: 1, project: "p", status: "approved", feature: "f", body: { title: "t", tasks: [] } },
    { url: "http://127.0.0.1:1", seed: null, tester: false, max_fixes: 1, up: "true", down: "true",
      checks: [{ name: "stubborn", run: `trap '' TERM; ${marker}`, timeout_s: 1 }] },
    dir, undefined, () => {});
  const took = Date.now() - started;
  assert.ok(took >= 5000 && took < 15_000, String(took));
  assert.match(r.checks[0].output, /timed out after 1s \(killed the process group\)/);
  assert.equal(alive(marker), false);
});

test("a check that exits leaves nothing running in the background", async () => {
  const marker = unique(8);
  const dir = mkdtempSync(join(tmpdir(), "smoke-proc-"));
  const r = await smokeTest(
    { id: 1, project: "p", status: "approved", feature: "f", body: { title: "t", tasks: [] } },
    { url: "http://127.0.0.1:1", seed: null, tester: false, max_fixes: 1, up: "true", down: "true",
      checks: [{ name: "bg", run: `${marker} >/dev/null 2>&1 &`, timeout_s: 10 }] },
    dir, undefined, () => {});
  assert.equal(r.checks[0].passed, true);
  assert.equal(alive(marker), false);
});

const task = { id: 4, body: { title: "t" } } as unknown as Task;
const gc = {
  version: 1,
  config: { gates: [], allowed_commands: [], retries: { max: 2, stop_on_repeat: true },
    budget: { task_tokens: 1e6, task_minutes: 5, plan_tokens: 1e6 }, review: { enabled: false, max_rounds: 1 }, smoke_test: null },
} as unknown as GateConfig;

// A fake `query` that starts its process the way the SDK does: through the `spawnClaudeCodeProcess` it is given.
const fakeSession = (command: string, args: string[], during?: () => void) => (async function* (a: { options: Options }) {
  a.options.spawnClaudeCodeProcess!({ command, args, env: process.env, signal: new AbortController().signal });
  during?.();
  await new Promise((r) => setTimeout(r, 400));
  yield { type: "system", subtype: "init", session_id: "s1" };
}) as never;

test("what a session leaves running is killed when it ends, and the log says so", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify(gc), { headers: { "content-type": "application/json" } });
  const leftover = unique(2).split(" ")[1];
  const log: string[] = [];
  await implement(task, "/wt", "main", { project: "p", repo: ".", maxTasks: 1, push: false, log: (l: string) => log.push(l),
    seams: { query: fakeSession("sleep", [leftover]) } } as unknown as RunOptions);
  assert.equal(alive(`sleep ${leftover}`), false);
  assert.ok(log.some((l) => /killed 1 leftover process$/.test(l)), log.join("\n"));
});

test("processes outside the session's tree are not touched, whether older or started during it", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify(gc), { headers: { "content-type": "application/json" } });
  const older = spawn("sleep", [unique(5).split(" ")[1]], { stdio: "ignore" });
  let concurrent: ChildProcess | undefined; // e.g. the docs agent or the handoff writer of another task
  const log: string[] = [];
  try {
    await implement(task, "/wt", "main", { project: "p", repo: ".", maxTasks: 1, push: false, log: (l: string) => log.push(l),
      seams: { query: fakeSession("sleep", [unique(2).split(" ")[1]], () => {
        concurrent = spawn("sleep", [unique(9).split(" ")[1]], { stdio: "ignore" });
      }) } } as unknown as RunOptions);
    assert.equal(older.exitCode, null);
    assert.equal(older.signalCode, null);
    assert.ok(concurrent);
    assert.equal(concurrent.exitCode, null);
    assert.equal(concurrent.signalCode, null);
    assert.ok(log.some((l) => /killed 1 leftover process$/.test(l)), log.join("\n"));
  } finally {
    older.kill();
    concurrent?.kill();
  }
});

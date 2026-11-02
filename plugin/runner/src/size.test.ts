import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import type { Run } from "./context.js";
import { codeLines, git } from "./git.js";
import { run } from "./run.js";

process.env.AUTONOME_CONTEXT_RETRY_MS = "1";
const realFetch = globalThis.fetch;
const realUrl = process.env.AUTONOME_CONTEXT_URL;
afterEach(() => {
  globalThis.fetch = realFetch;
  if (realUrl === undefined) delete process.env.AUTONOME_CONTEXT_URL; else process.env.AUTONOME_CONTEXT_URL = realUrl;
  delete process.env.AUTONOME_CONTEXT_WAIT_S;
});

const lines = (n: number) => Array.from({ length: n }, (_, i) => `line ${i}\n`).join("");

function makeRepo() {
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "autonome-size-")));
  git(repo, "init", "-q", "-b", "main");
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  writeFileSync(join(repo, "a.txt"), "a\n");
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "init");
  return repo;
}

/** 200 lines of code, 500 of tests, 100 of docs and a binary file. */
function writeWork(dir: string) {
  for (const d of ["src", "tests", "docs"]) mkdirSync(join(dir, d), { recursive: true });
  writeFileSync(join(dir, "src", "app.py"), lines(200));
  writeFileSync(join(dir, "tests", "test_app.py"), lines(300));
  writeFileSync(join(dir, "src", "app.test.ts"), lines(200));
  writeFileSync(join(dir, "docs", "guide.txt"), lines(60));
  writeFileSync(join(dir, "README.md"), lines(40));
  writeFileSync(join(dir, "logo.bin"), Buffer.from([0, 1, 2, 0, 255, 0, 3]));
}

test("codeLines counts code only: tests, docs and binaries are left out", () => {
  const repo = makeRepo();
  const base = git(repo, "rev-parse", "HEAD");
  writeWork(repo);
  git(repo, "add", ".");
  git(repo, "commit", "-qm", "work");
  assert.equal(codeLines(repo, base), 200);
});

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
const task = { id: 9, project: "p", status: "running", feature: "links", layers: [],
  body: { title: "Links", spec: "adds logo.bin", acceptance: [], plan_id: 16, seq: 1, hold_before_merge: false, runs: [] } };

async function play(repo: string, config: Record<string, unknown>, measure?: typeof codeLines) {
  const transitions: { to: string; run?: Run }[] = [];
  const log: string[] = [];
  let claimed = false;
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/health")) return json({ status: "ok" });
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/tasks/claim")) {
      process.env.AUTONOME_CONTEXT_URL = "http://127.0.0.1:9";
      process.env.AUTONOME_CONTEXT_WAIT_S = "0";
      const first = !claimed;
      claimed = true;
      return json(first ? { task, preflight: null, smoke_test: null } : { task: null, preflight: null, smoke_test: null });
    }
    if (url.endsWith("/state")) return json({ paused: null });
    if (url.endsWith("/gate-config")) return json({ config: {
      gates: [{ name: "t", run: "true" }], retries: { max: 1, stop_on_repeat: true },
      budget: { task_tokens: 1e6, task_minutes: 5, plan_tokens: 1e6 }, review: { enabled: false, max_rounds: 1 }, smoke_test: null, ...config } });
    if (url.includes("/briefing")) return json({ text: "briefing" });
    if (url.includes("/transition")) {
      transitions.push(JSON.parse(String(init?.body)));
      return json({ ok: true });
    }
    if (url.endsWith("/waves/without-retro")) return json([]);
    return json({});
  };
  const query = ((args: { options: any }) => (async function* () {
    yield { type: "system", subtype: "init", session_id: "s-new" };
    writeWork(args.options.cwd);
    yield { type: "assistant", message: { usage: { input_tokens: 1, output_tokens: 1 }, content: [] } };
    yield { type: "result", subtype: "success", total_cost_usd: 0 };
  })()) as never;
  const error = await run({ project: "p", repo, maxTasks: 5, push: false, log: (l) => log.push(l),
    seams: { query, codeLines: measure, gates: () => ({ passed: true, summary: "all gates passed" }) } }).then(() => undefined, (e: Error) => e);
  return { transitions, log, error };
}

test("a task over task_size.max_lines is merged, flagged oversize and logged", async () => {
  const repo = makeRepo();
  const { transitions, log, error } = await play(repo, { task_size: { max_lines: 100 } });
  assert.equal(error, undefined);
  assert.deepEqual(transitions.map((t) => t.to), ["done"], log.join("\n"));
  const r = transitions[0].run!;
  assert.equal(r.outcome, "merged");
  assert.equal(r.code_lines, 200);
  assert.equal(r.oversize, true);
  assert.equal(r.max_lines, 100);
  assert.ok(log.includes("  task oversize: 200 > 100"));
});

test("a git diff that fails is a warning in the log and the task is still merged", async () => {
  const repo = makeRepo();
  const { transitions, log } = await play(repo, { task_size: { max_lines: 100 } }, () => { throw new Error("fatal: bad revision"); });
  assert.deepEqual(transitions.map((t) => t.to), ["done"], log.join("\n"));
  const r = transitions[0].run!;
  assert.equal(r.outcome, "merged");
  assert.equal(r.code_lines, undefined);
  assert.equal(r.oversize, undefined);
  assert.ok(log.some((l) => l.includes("warning: could not measure")));
});

test("a task within the limit is merged without the flag", async () => {
  const repo = makeRepo();
  const { transitions, log } = await play(repo, { task_size: { max_lines: 300 } });
  const r = transitions[0].run!;
  assert.equal(r.code_lines, 200);
  assert.equal(r.oversize, undefined);
  assert.ok(!log.some((l) => l.includes("oversize")));
});

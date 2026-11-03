import assert from "node:assert/strict";
import { test } from "node:test";
import { DocsQueue, type RunOptions } from "./run.js";

type Docs = NonNullable<NonNullable<RunOptions["seams"]>["docs"]>;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function queue(docs: Docs, docsWaitMs?: number) {
  const log: string[] = [];
  const opts: RunOptions = { project: "p", repo: "/nowhere", maxTasks: 1, push: false, log: (l) => log.push(l), seams: { docs }, docsWaitMs };
  return { q: new DocsQueue(opts), log };
}

test("a merge only marks the feature: its doc is written at the drain", async () => {
  let calls = 0;
  const { q } = queue(async () => { calls++; });
  q.start("f");
  await sleep(20);
  assert.equal(calls, 0); // nothing runs while tasks are still playing
  await q.drain();
  assert.equal(calls, 1);
});

test("many merges of a feature before the drain make one doc", async () => {
  const seen: string[] = [];
  const { q } = queue(async (_r, _p, args) => { seen.push(args[1]); });
  q.start("f");
  q.start("f");
  q.start("f");
  q.start("other");
  await q.drain();
  assert.deepEqual(seen.sort(), ["f", "other"]);
});

test("a merge while its doc runs makes exactly one more doc", async () => {
  let calls = 0;
  let release: () => void = () => {};
  const { q } = queue(async () => {
    calls++;
    if (calls === 1) await new Promise<void>((r) => (release = r));
  });
  q.start("f");
  const drained = q.drain();
  await sleep(10);
  q.start("f");
  q.start("f");
  release();
  await drained;
  assert.equal(calls, 2);
});

test("the same feature never runs twice at once", async () => {
  let active = 0;
  let peak = 0;
  const { q } = queue(async () => {
    peak = Math.max(peak, ++active);
    await sleep(30);
    active--;
  });
  q.start("f");
  q.start("f");
  await q.drain();
  assert.equal(peak, 1);
});

test("drain waits for the pending doc before the runner leaves", async () => {
  let finished = false;
  const { q } = queue(async () => { await sleep(100); finished = true; }, 2000);
  q.start("f");
  await q.drain();
  assert.equal(finished, true);
});

test("a stuck doc is killed after the ceiling and logged", async () => {
  let killed = false;
  const { q, log } = queue((_r, _p, _a, _l, signal) => new Promise<void>((resolve) => {
    signal?.addEventListener("abort", () => { killed = true; resolve(); });
  }), 50);
  q.start("f");
  const t0 = Date.now();
  await q.drain();
  assert.ok(Date.now() - t0 < 1000);
  assert.equal(killed, true);
  assert.ok(log.some((l) => l.includes("docs left for the doc-drift schedule")));
});

test("a failing doc only logs", async () => {
  const { q, log } = queue(async () => { throw new Error("boom"); });
  q.start("f");
  await q.drain();
  assert.ok(log.some((l) => l.includes("docs update failed: Error: boom")));
});

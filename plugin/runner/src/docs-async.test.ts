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

test("start returns at once: the next task begins before the doc ends", async () => {
  let finished = false;
  const { q } = queue(async () => { await sleep(80); finished = true; });
  q.start("f");
  assert.equal(finished, false); // the caller is free to claim the next task
  await q.drain();
  assert.equal(finished, true);
});

test("two merges of a feature while its doc runs make exactly one more doc", async () => {
  let calls = 0;
  let active = 0;
  let overlap = false;
  const { q } = queue(async () => {
    calls++;
    if (++active > 1) overlap = true;
    await sleep(40);
    active--;
  });
  q.start("f");
  q.start("f");
  q.start("f");
  q.start("other"); // another feature does not wait
  await q.drain();
  assert.equal(calls, 3); // f, f again, other
  assert.equal(overlap, true); // f and other ran together, never f with f
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

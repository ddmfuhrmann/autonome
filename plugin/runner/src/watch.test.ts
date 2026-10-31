import { test } from "node:test";
import assert from "node:assert/strict";
import { changes, summary, type State } from "./watch.js";

const empty: State = { tasks: [], plans: [], waves: [], waiting: [] };

test("a task moving and a wave finishing are info lines", () => {
  const prev: State = { ...empty, tasks: [{ id: 1, title: "A", status: "pending" }, { id: 2, title: "B", status: "playing" }],
    waves: [{ id: 9, title: "W1", status: "approved" }] };
  const next: State = { ...empty, tasks: [{ id: 1, title: "A", status: "playing" }, { id: 2, title: "B", status: "done" }],
    waves: [{ id: 9, title: "W1", status: "done" }] };
  assert.deepEqual(changes(prev, next), ["info T-1 started: A", "info T-2 merged: B", "info W-9 finished: W1"]);
});

test("new waiting items say whether they block, with the blocked reason", () => {
  const next: State = { ...empty, tasks: [{ id: 3, title: "C", status: "blocked", reason: "metronome: build\nmore" }],
    waiting: [{ kind: "blocked", id: 3, title: "C", blocks: true }, { kind: "playback", id: 4, title: "D", blocks: false }] };
  assert.deepEqual(changes(empty, next), [
    "needs-you #3 blocked: C (metronome: build)",
    "review #4 merged, playback to hear: D",
  ]);
  assert.deepEqual(changes(next, next), []);
});

test("the summary counts tasks and what waits", () => {
  const s: State = { ...empty, tasks: [{ id: 1, title: "A", status: "done" }],
    waiting: [{ kind: "plans", id: 5, title: "P", blocks: true }] };
  assert.equal(summary(s), "watching: 0 playing, 0 pending, 1 done; 1 waiting on you, 0 to review when you like");
});

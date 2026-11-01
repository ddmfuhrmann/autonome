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

test("a held plan the triage judged hold shows the flag that weighed most", () => {
  const next: State = { ...empty, triage: true, waiting: [
    { kind: "held", id: 677, title: "shortlink-v6", blocks: true, triage_hold: "changes_design" },
    { kind: "held", id: 678, title: "no verdict", blocks: true, triage_hold: null },
  ] };
  assert.deepEqual(changes(empty, next), [
    "needs-you #677 held, triage hold: changes_design",
    "needs-you #678 held by rehearsal: no verdict",
  ]);
});

test("with the triage on, only a deviation it could not settle is reported", () => {
  const next: State = { ...empty, triage: true, waiting: [
    { kind: "decision", id: 1, title: "judged soon", blocks: false, deviation: true },
    { kind: "decision", id: 2, title: "gave up", blocks: false, deviation: true, exhausted: true },
    { kind: "decision", id: 3, title: "plain choice", blocks: false },
  ] };
  assert.deepEqual(changes(empty, next), [
    "review #2 deviation the triage could not settle: gave up",
    "review #3 decision to review: plain choice",
  ]);
});

test("with the triage on, a rule-conflict deviation is a review item and judged ones stay hidden", () => {
  const next: State = { ...empty, triage: true, waiting: [
    { kind: "decision", id: 12, title: "noqa for a forbidden name", blocks: false, deviation: true, conflict: true },
    { kind: "decision", id: 13, title: "judged soon", blocks: false, deviation: true },
  ] };
  assert.deepEqual(changes(empty, next), ["review #12 rule conflict: noqa for a forbidden name"]);
});

test("with the triage off, a deviation is a decision to review", () => {
  const next: State = { ...empty, waiting: [{ kind: "decision", id: 1, title: "d", blocks: false, deviation: true }] };
  assert.deepEqual(changes(empty, next), ["review #1 decision to review: d"]);
});

test("the summary counts tasks and what waits", () => {
  const s: State = { ...empty, tasks: [{ id: 1, title: "A", status: "done" }],
    waiting: [{ kind: "plans", id: 5, title: "P", blocks: true }] };
  assert.equal(summary(s), "watching: 0 playing, 0 pending, 1 done; 1 waiting on you, 0 to review when you like");
});

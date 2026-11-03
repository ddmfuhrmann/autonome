import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Task } from "./context.js";
import { STAFF_SCHEMA, staffDiagnosis, staffEligible } from "./staff.js";

function repo() {
  const dir = mkdtempSync(join(tmpdir(), "staff-"));
  const g = (...a: string[]) => execFileSync("git", a, { cwd: dir, encoding: "utf8" }).trim();
  g("init", "-q", "-b", "main");
  g("config", "user.email", "t@t");
  g("config", "user.name", "t");
  writeFileSync(join(dir, "a.txt"), "a\n");
  g("add", ".");
  g("commit", "-qm", "first commit");
  const base = g("rev-parse", "HEAD");
  writeFileSync(join(dir, "b.txt"), "b\n");
  g("add", ".");
  return { dir, base };
}

const task = { id: 7, body: { title: "Do it", spec: "the spec", acceptance: ["one", "two"] } } as unknown as Task;

test("the diagnosis has what the staff needs, with the gates cut at 6000 and the messages at 3000", () => {
  const { dir, base } = repo();
  const text = staffDiagnosis({ task, failure: "lint failed: x", briefing: "THE BRIEFING", gates: "g".repeat(7000) + "END",
    findings: "- high bug: y", said: "START" + "s".repeat(3500), repo: dir, worktree: dir, base });
  for (const want of ["Do it", "the spec", "- one", "- two", "THE BRIEFING", "lint failed: x", "- high bug: y", "first commit", "b.txt"]) {
    assert.ok(text.includes(want), want);
  }
  assert.ok(text.includes("g".repeat(6000)) && !text.includes("g".repeat(6001)), "gates cut at 6000");
  assert.ok(!text.includes("END"));
  assert.ok(!text.includes("START"), "messages keep the last 3000");
  assert.ok(text.includes("s".repeat(3000)));
});

test("the schema has the five actions and a bounded diagnosis", () => {
  assert.deepEqual(STAFF_SCHEMA.properties.action.enum, ["fixed", "fix_rule", "already_satisfied", "revise", "give_up"]);
  assert.deepEqual(STAFF_SCHEMA.properties.rule_change.required, ["files", "why"]);
  assert.equal(STAFF_SCHEMA.properties.diagnosis.minLength, 1);
  assert.equal(STAFF_SCHEMA.properties.diagnosis.maxLength, 2000);
});

test("who is not for the staff: usage limit, runner error, main, pull request, a second look", () => {
  const staffed = { staff: { action: "fixed", diagnosis: "d", model: "m", cost_usd: 0 } };
  for (const f of ["the session changed nothing", "gate hook: x", "lint failed: x", "error_max_turns",
    "stray files still in the change:\n- a", "review: still blocking after 2 retries:\n- x"]) {
    assert.ok(staffEligible(f, {}), f);
    assert.ok(!staffEligible(f, staffed), f);
  }
  for (const f of ["You've hit your session limit", "runner error: boom", "x conflicts with main", "pull request closed without merge"]) {
    assert.ok(!staffEligible(f, {}), f);
  }
});

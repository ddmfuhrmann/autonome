import assert from "node:assert/strict";
import { test } from "node:test";
import { isUsageLimit, untilReset } from "./limits.js";

test("the account's usage limit is told apart from a task's own failure", () => {
  assert.ok(isUsageLimit("Error: Claude Code returned an error result: You've hit your session limit · resets 3:40pm (America/Sao_Paulo)"));
  assert.ok(isUsageLimit("usage limit reached"));
  assert.ok(!isUsageLimit("token budget exceeded (500000 > 400000)"));
  assert.ok(!isUsageLimit("gates failed on top of main: pytest failed"));
});

test("the conductor waits until the reset the message names, plus a minute", () => {
  const now = new Date(2026, 9, 31, 15, 10, 0);
  assert.equal(untilReset("You've hit your session limit · resets 3:40pm", now), 31 * 60_000);
  assert.equal(untilReset("resets 9am", new Date(2026, 9, 31, 22, 0, 0)), 6 * 3_600_000); // capped
  assert.equal(untilReset("limit reached", now), 15 * 60_000);
});

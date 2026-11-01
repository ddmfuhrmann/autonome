import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { runSchedule } from "./schedules.js";

test("the Jev key does not reach a schedule's command", async () => {
  const was = process.env.TYPESAFE_API_KEY;
  process.env.TYPESAFE_API_KEY = "secret";
  try {
    const dir = mkdtempSync(join(tmpdir(), "schedules-"));
    // prints nothing and exits 0 only when the key is absent: "clean"; otherwise it would queue a task
    const out = await runSchedule({ name: "env", kind: "command", every_hours: 1, run: 'test -z "$TYPESAFE_API_KEY"' }, dir,
      { project: "p", repo: dir, force: true, log: () => {} });
    assert.equal(out, "clean");
  } finally {
    if (was === undefined) delete process.env.TYPESAFE_API_KEY;
    else process.env.TYPESAFE_API_KEY = was;
  }
});

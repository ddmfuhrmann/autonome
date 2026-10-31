import assert from "node:assert/strict";
import { test } from "node:test";
import { verdictLine } from "./conduct.js";

test("the reviewer's verdict reads as a short list in the live view", () => {
  assert.equal(verdictLine({ verdict: "pass", findings: [] }), "**pass**, no findings");
  assert.equal(verdictLine({ verdict: "pass", findings: [
    { kind: "missing", severity: "low", issue: "No note on\nthe check.", file: "gradle/libs.versions.toml" }] }),
    "**pass**, 1 finding:\n- low missing in `gradle/libs.versions.toml`: No note on the check.");
});

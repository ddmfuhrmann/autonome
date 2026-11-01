// The runner's side of contracts/runner-context.json: each body it POSTs to the context is built from
// the fixture's input and must match the fixture exactly. The context's tests validate the same bodies
// with its models, so a field one side drops or renames fails a gate instead of a live run.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { revisionBody, screeningBody, verdictBody } from "./triage-run.js";

const CONTRACT = JSON.parse(readFileSync(new URL("../../../contracts/runner-context.json", import.meta.url), "utf8"));

test("the deviation verdict body matches the contract, with the round as the context counts it", () => {
  const c = CONTRACT.triage_verdict;
  assert.deepEqual(verdictBody(c.item, c.judgment), c.body);
  assert.ok(c.item.round >= 1, "the context counts rounds from 1");
});

test("the screening body matches the contract", () => {
  const c = CONTRACT.screening;
  assert.deepEqual(screeningBody(c.screening, c.reject_at), c.body);
});

test("the revision verdict body matches the contract, state included", () => {
  const c = CONTRACT.revision_verdict;
  assert.deepEqual(revisionBody(c.item, c.judgment), c.body);
});

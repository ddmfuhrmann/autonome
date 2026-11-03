import { test } from "node:test";
import assert from "node:assert/strict";
import { NO_MODELS, query } from "./models.js";

test("with AUTONOME_NO_MODELS set, a real SDK session throws before it starts", () => {
  const saved = process.env[NO_MODELS];
  process.env[NO_MODELS] = "1";
  try {
    assert.throws(() => query({ prompt: "hi" }), /AUTONOME_NO_MODELS/);
  } finally {
    if (saved === undefined) delete process.env[NO_MODELS];
    else process.env[NO_MODELS] = saved;
  }
});

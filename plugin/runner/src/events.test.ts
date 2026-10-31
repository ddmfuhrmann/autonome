import assert from "node:assert/strict";
import { test } from "node:test";
import { describeTool, EventStream, fromMessage } from "./events.js";

test("SDK messages become short live events: words, tool calls, errors and results", () => {
  const said = fromMessage({ type: "assistant", message: { content: [
    { type: "text", text: "Adding the ledger entry.\n\nThen the test." },
    { type: "tool_use", name: "Bash", input: { command: "uv run pytest -q" } },
    { type: "tool_use", name: "mcp__plugin_autonome_autonome__search", input: { query: "ledger" } },
  ] } });
  assert.deepEqual(said, [
    { kind: "say", text: "Adding the ledger entry. Then the test." },
    { kind: "tool", text: "Bash: uv run pytest -q" },
    { kind: "tool", text: "memory:search: ledger" },
  ]);
  const results = fromMessage({ type: "user", message: { content: [
    { type: "tool_result", content: "Permission to use Bash has been denied", is_error: true },
    { type: "tool_result", content: [{ type: "text", text: "12 passed in 0.31s" }] },
  ] } });
  assert.deepEqual(results.map((e) => e.kind), ["error", "result"]);
  assert.deepEqual(fromMessage({ type: "system", subtype: "init" }), []);
});

test("a tool's input is summarised by what it acts on", () => {
  assert.equal(describeTool("Edit", { file_path: "src/ledger.py", old_string: "x" }), "Edit: src/ledger.py");
  assert.equal(describeTool("TodoWrite", { todos: [] }), "TodoWrite");
});

test("events go in batches, and a failing context never breaks the run", async () => {
  const sent: unknown[] = [];
  const stream = new EventStream("p", async (body) => { sent.push(body); });
  stream.push({ task_id: 1, role: "musician", kind: "tool", text: "Bash: ls" });
  stream.push({ task_id: 1, role: "maestro", kind: "stage", text: "gates" });
  await stream.flush();
  assert.equal(sent.length, 1);
  assert.deepEqual((sent[0] as { events: { text: string }[] }).events.map((e) => e.text), ["Bash: ls", "gates"]);
  const broken = new EventStream("p", async () => { throw new Error("context down"); });
  broken.push({ role: "maestro", kind: "stage", text: "x" });
  await broken.flush(); // no throw
});

import assert from "node:assert/strict";
import { test } from "node:test";
import { describeTool, EventStream, fromMessage } from "./events.js";

test("SDK messages become short live events: words, tool calls, errors and results", () => {
  const said = fromMessage({ type: "assistant", message: { content: [
    { type: "text", text: "Adding the ledger entry.\n\nThen the test." },
    { type: "tool_use", id: "b1", name: "Bash", input: { command: "uv run pytest -q" } },
    { type: "tool_use", name: "mcp__plugin_autonome_autonome__search", input: { query: "ledger" } },
  ] } });
  assert.deepEqual(said, [
    { kind: "say", text: "Adding the ledger entry.\n\nThen the test." },
    { kind: "tool", text: "Bash: uv run pytest -q" },
    { kind: "tool", text: "memory:search: ledger" },
  ]);
  const results = fromMessage({ type: "user", message: { content: [
    { type: "tool_result", content: "Permission to use Bash has been denied", is_error: true },
    { type: "tool_result", tool_use_id: "b1", content: [{ type: "text", text: "12 passed in 0.31s" }] },
  ] } });
  assert.deepEqual(results.map((e) => e.kind), ["error", "result"]);
  assert.deepEqual(fromMessage({ type: "system", subtype: "init" }), []);
});

test("only command output is shown; written and read files and the reviewer's structured output are not", () => {
  const wt = "/Users/me/git/.autonome-worktrees/autonome-task-9-abc";
  const calls = fromMessage({ type: "assistant", message: { content: [
    { type: "tool_use", id: "w", name: "Write", input: { file_path: `${wt}/src/a.ts` } },
    { type: "tool_use", id: "r", name: "Read", input: { file_path: `${wt}/src/b.ts` } },
    { type: "tool_use", id: "s", name: "StructuredOutput", input: { verdict: "pass" } },
    { type: "tool_use", id: "g", name: "Bash", input: { command: "./gradlew test" } },
  ] } });
  assert.deepEqual(calls.map((e) => e.text), ["Write: src/a.ts", "Read: src/b.ts", "Bash: ./gradlew test"]);
  const results = fromMessage({ type: "user", message: { content: [
    { type: "tool_result", tool_use_id: "w", content: `File created successfully at: ${wt}/src/a.ts` },
    { type: "tool_result", tool_use_id: "r", content: "1 package x;" },
    { type: "tool_result", tool_use_id: "s", content: "Structured output provided successfully" },
    { type: "tool_result", tool_use_id: "g", content: "> Task :compileJava\n" + "x".repeat(400) + "\nBUILD SUCCESSFUL in 9s" },
  ] } });
  assert.equal(results.length, 1);
  assert.ok(results[0].text.startsWith("…") && results[0].text.endsWith("BUILD SUCCESSFUL in 9s"));
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

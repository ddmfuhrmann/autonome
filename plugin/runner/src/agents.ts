// Read-only agents the maestro consults: they look, never change the code, and answer as JSON.
import { query } from "@anthropic-ai/claude-agent-sdk";
import { CONTEXT_URL, type Effort } from "./context.js";

const READ_ONLY = [
  "Read", "Glob", "Grep",
  "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)", "Bash(git status:*)",
  "mcp__autonome__search", "mcp__autonome__plan_get", "mcp__autonome__conventions_get", "mcp__autonome__history_get",
];

export interface Consult {
  cwd: string;
  prompt: string;
  role: string;
  schema: Record<string, unknown>;
  model?: string;
  effort?: Effort;
  /** Extra tools on top of reading files, git and the project memory. */
  tools?: string[];
  env?: Record<string, string>;
  maxTurns?: number;
}

/** Runs one read-only session and returns its structured answer. */
export async function consult<T>(c: Consult): Promise<{ answer: T; cost: number }> {
  let answer: T | undefined;
  let cost = 0;
  for await (const msg of query({
    prompt: c.prompt,
    options: {
      cwd: c.cwd,
      model: c.model,
      effort: c.effort,
      permissionMode: "dontAsk",
      allowedTools: [...READ_ONLY, ...(c.tools ?? [])],
      mcpServers: { autonome: { type: "http", url: `${CONTEXT_URL}/mcp` } },
      settingSources: ["project"],
      systemPrompt: { type: "preset", preset: "claude_code", append: c.role },
      outputFormat: { type: "json_schema", schema: c.schema },
      maxTurns: c.maxTurns ?? 40,
      env: { ...process.env, ...(c.env ?? {}) } as Record<string, string>,
    },
  })) {
    if (msg.type === "result") {
      cost = msg.total_cost_usd;
      if (msg.subtype === "success") answer = msg.structured_output as T;
    }
  }
  if (answer === undefined) throw new Error("the agent returned no answer");
  return { answer, cost };
}

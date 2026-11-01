// Live view of what sessions are doing: the runner turns SDK messages and its own stages into short
// events and sends them to the context in small batches. Best effort: losing events never stops a run.
import { call } from "./context.js";

export interface LiveEvent {
  task_id?: number;
  plan_id?: number;
  role: string; // implementer, reviewer, preflight, tester, retro, runner
  kind: string; // say, tool, result, error, stage
  text: string;
  at?: string;
}

const short = (text: string, n: number) => {
  const one = text.replace(/\s+/g, " ").trim();
  return one.length > n ? `${one.slice(0, n - 1)}…` : one;
};

/** Worktree paths read as repo paths: `/…/.autonome-worktrees/task-1-x/src/a.ts` becomes `src/a.ts`. */
export const relative = (text: string) => text.replace(/\S*\/\.autonome-worktrees\/[^/\s]+\//g, "");

/** The tools whose calls are still waiting for a result, by tool_use id: only some results are worth showing. */
const calls = new Map<string, string>();
// Results worth a line: what a command printed. A written file or a read file's contents is noise.
const SHOWN = new Set(["Bash"]);

/** What a tool call was about, in one line: the command, the file, the pattern. */
export function describeTool(name: string, input: Record<string, unknown>): string {
  const pick = ["command", "file_path", "pattern", "path", "url", "query", "description"]
    .map((k) => input[k]).find((v) => typeof v === "string") as string | undefined;
  const tool = name.replace(/^mcp__(plugin_autonome_)?autonome__/, "memory:");
  return pick ? `${tool}: ${short(relative(pick), 200)}` : tool;
}

/** SDK messages as events: what the agent says (newlines kept, it is markdown), the tools it calls,
 * failures, and the end of what commands printed. The reviewer's StructuredOutput is left out: the run
 * reports the verdict itself. */
export function fromMessage(msg: unknown): Omit<LiveEvent, "role" | "task_id" | "plan_id">[] {
  const m = msg as { type?: string; message?: { content?: unknown } };
  const content = Array.isArray(m.message?.content) ? (m.message!.content as Record<string, unknown>[]) : [];
  const out: Omit<LiveEvent, "role" | "task_id" | "plan_id">[] = [];
  for (const block of content) {
    if (m.type === "assistant" && block.type === "text" && typeof block.text === "string" && block.text.trim()) {
      const text = relative(block.text.trim()).replace(/\n{3,}/g, "\n\n");
      out.push({ kind: "say", text: text.length > 1200 ? `${text.slice(0, 1199)}…` : text });
    } else if (m.type === "assistant" && block.type === "tool_use") {
      const name = String(block.name);
      calls.set(String(block.id), name);
      if (name !== "StructuredOutput") {
        out.push({ kind: "tool", text: describeTool(name, (block.input ?? {}) as Record<string, unknown>) });
      }
    } else if (m.type === "user" && block.type === "tool_result") {
      const name = calls.get(String(block.tool_use_id));
      calls.delete(String(block.tool_use_id));
      const raw = relative(typeof block.content === "string" ? block.content
        : Array.isArray(block.content) ? (block.content as { text?: string }[]).map((c) => c.text ?? "").join("\n") : "");
      const tail = (n: number) => { const one = raw.replace(/\s+/g, " ").trim(); return one.length > n ? `…${one.slice(-n)}` : one; };
      if (block.is_error && name !== "StructuredOutput") out.push({ kind: "error", text: tail(300) });
      else if (name && SHOWN.has(name) && raw.trim()) out.push({ kind: "result", text: tail(240) });
    }
  }
  return out;
}

export class EventStream {
  private queue: LiveEvent[] = [];
  private timer: NodeJS.Timeout | undefined;

  constructor(private project: string, private send = (body: unknown) =>
    call(`/api/events`, { method: "POST", body: JSON.stringify(body) }, 1)) {}

  push(e: LiveEvent): void {
    this.queue.push({ ...e, at: e.at ?? new Date().toISOString() });
    this.timer ??= setTimeout(() => void this.flush(), 2000);
  }

  async flush(): Promise<void> {
    clearTimeout(this.timer);
    this.timer = undefined;
    const batch = this.queue.splice(0);
    if (batch.length) await this.send({ project: this.project, events: batch }).catch(() => undefined);
  }
}

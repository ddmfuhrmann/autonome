// Live view of what sessions are doing: the runner turns SDK messages and its own stages into short
// events and sends them to the context in small batches. Best effort: losing events never stops a run.
import { call } from "./context.js";

export interface LiveEvent {
  task_id?: number;
  plan_id?: number;
  role: string; // musician, reviewer, rehearsal, listener, tune, maestro
  kind: string; // say, tool, result, error, stage
  text: string;
  at?: string;
}

const short = (text: string, n: number) => {
  const one = text.replace(/\s+/g, " ").trim();
  return one.length > n ? `${one.slice(0, n - 1)}…` : one;
};

/** What a tool call was about, in one line: the command, the file, the pattern. */
export function describeTool(name: string, input: Record<string, unknown>): string {
  const pick = ["command", "file_path", "pattern", "path", "url", "query", "description"]
    .map((k) => input[k]).find((v) => typeof v === "string") as string | undefined;
  const tool = name.replace(/^mcp__(plugin_autonome_)?autonome__/, "memory:");
  return pick ? `${tool}: ${short(pick, 200)}` : tool;
}

/** SDK messages as events: what the agent says, the tools it calls, and failed or short results. */
export function fromMessage(msg: unknown): Omit<LiveEvent, "role" | "task_id" | "plan_id">[] {
  const m = msg as { type?: string; message?: { content?: unknown } };
  const content = Array.isArray(m.message?.content) ? (m.message!.content as Record<string, unknown>[]) : [];
  const out: Omit<LiveEvent, "role" | "task_id" | "plan_id">[] = [];
  for (const block of content) {
    if (m.type === "assistant" && block.type === "text" && typeof block.text === "string" && block.text.trim()) {
      out.push({ kind: "say", text: short(block.text, 600) });
    } else if (m.type === "assistant" && block.type === "tool_use") {
      out.push({ kind: "tool", text: describeTool(String(block.name), (block.input ?? {}) as Record<string, unknown>) });
    } else if (m.type === "user" && block.type === "tool_result") {
      const raw = typeof block.content === "string" ? block.content
        : Array.isArray(block.content) ? (block.content as { text?: string }[]).map((c) => c.text ?? "").join(" ") : "";
      if (block.is_error) out.push({ kind: "error", text: short(raw, 300) });
      else if (raw.trim()) out.push({ kind: "result", text: short(raw, 200) });
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

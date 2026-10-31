// Client for autonome-context's HTTP API.

export const CONTEXT_URL = process.env.AUTONOME_CONTEXT_URL ?? "http://127.0.0.1:8765";

export interface Task {
  id: number;
  project: string;
  status: string;
  feature: string | null;
  layers: string[];
  body: {
    title: string;
    spec: string;
    acceptance: string[];
    plan_id: number;
    seq: number;
    fermata: boolean;
    runs?: Run[];
  };
}

export interface Run {
  at: string;
  session_id?: string;
  branch: string;
  worktree: string;
  base: string;
  outcome: string;
  reason?: string;
  tokens?: number;
  cost_usd?: number;
  diff_stat?: string;
  diff?: string;
}

export interface GateConfig {
  version: number | null;
  config: {
    gates: { name: string; run: string }[];
    allowed_commands?: string[];
    encore: { max: number; stop_on_repeat: boolean };
    budget: { task_tokens: number; task_minutes: number; plan_tokens: number };
  };
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${CONTEXT_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = (await res.json()) as T & { error?: string };
  if (!res.ok) throw new Error(`${init?.method ?? "GET"} ${path}: ${res.status} ${body.error ?? ""}`);
  return body;
}

export const context = {
  health: () => call<{ status: string }>("/health"),
  claim: (project: string) =>
    call<{ task: Task | null }>(`/api/projects/${project}/tasks/claim`, { method: "POST", body: "{}" }),
  transition: (taskId: number, to: string, run?: Run) =>
    call<{ status: string }>(`/api/tasks/${taskId}/transition`, {
      method: "POST",
      body: JSON.stringify({ to, run }),
    }),
  gateConfig: (project: string) => call<GateConfig>(`/api/projects/${project}/gate-config`),
};

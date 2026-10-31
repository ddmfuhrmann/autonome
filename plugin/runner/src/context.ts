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
    after_plans?: number[];
    wave_id?: number;
    runs?: Run[];
  };
}

export interface Plan {
  id: number;
  project: string;
  status: string;
  feature: string | null;
  body: Record<string, unknown> & { title: string; tasks: unknown[] };
}

export interface Rehearsal {
  verdict: "ready" | "revise";
  notes: string;
  plan?: Record<string, unknown>;
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
    cues?: Cue[];
  };
}

export interface Cue {
  name: string;
  kind: "command" | "doc-drift" | "main-red";
  every_hours: number;
  run?: string | null;
  task?: string | null;
}

export interface Feature {
  slug: string;
  handoffs: number;
  documented: boolean;
  last_work_at: string | null;
  doc_updated_at: string | null;
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
    call<{ task: Task | null; rehearse: Plan | null }>(`/api/projects/${project}/tasks/claim`, { method: "POST", body: "{}" }),
  rehearsal: (planId: number, r: Rehearsal) =>
    call<{ plan_status: string }>(`/api/plans/${planId}/rehearsal`, { method: "POST", body: JSON.stringify(r) }),
  transition: (taskId: number, to: string, run?: Run) =>
    call<{ status: string }>(`/api/tasks/${taskId}/transition`, {
      method: "POST",
      body: JSON.stringify({ to, run }),
    }),
  gateConfig: (project: string) => call<GateConfig>(`/api/projects/${project}/gate-config`),
  features: (project: string) => call<Feature[]>(`/api/projects/${project}/features`),
  enqueue: (project: string, task: { title: string; spec: string; source: string; feature?: string }) =>
    call<{ task_id: number; plan_id: number; plan_status?: string; duplicate: boolean }>(
      `/api/projects/${project}/tasks`, { method: "POST", body: JSON.stringify(task) }),
};

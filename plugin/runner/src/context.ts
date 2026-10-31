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
  review?: ReviewResult;
  review_rounds?: number;
  duration_s?: number;
}

export interface GateConfig {
  version: number | null;
  config: {
    gates: { name: string; run: string }[];
    allowed_commands?: string[];
    encore: { max: number; stop_on_repeat: boolean };
    budget: { task_tokens: number; task_minutes: number; plan_tokens: number };
    cues?: Cue[];
    review: { enabled: boolean; max_rounds: number };
    tune_after_wave?: boolean;
    soundcheck: Soundcheck | null;
  };
}

export interface Soundcheck {
  up: string;
  down: string;
  url: string;
  seed: string | null;
  checks: { name: string; run: string; timeout_s: number }[];
  listener: boolean;
  max_fixes: number;
}

export interface Fix {
  title: string;
  spec: string;
  acceptance: string[];
}

export interface SoundcheckResult {
  passed: boolean;
  report: string;
  checks: { name: string; passed: boolean; output: string }[];
  fixes: Fix[];
}

export interface Finding {
  kind?: "diverges" | "missing";
  severity: "high" | "medium" | "low";
  convention?: string;
  file?: string;
  issue: string;
}

export interface ReviewResult {
  verdict: "pass" | "fail";
  findings: Finding[];
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

/** The context API failed (down, 5xx, not JSON): an infrastructure problem, never the task's fault. */
export class ContextError extends Error {}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Polls /health with backoff until the context answers, for up to AUTONOME_CONTEXT_WAIT_S (300 s). */
export async function waitForContext(limitS = Number(process.env.AUTONOME_CONTEXT_WAIT_S ?? 300)): Promise<boolean> {
  const deadline = Date.now() + limitS * 1000;
  let delay = Number(process.env.AUTONOME_CONTEXT_RETRY_MS ?? 1000);
  while (Date.now() < deadline) {
    try {
      if ((await fetch(`${CONTEXT_URL}/health`)).ok) return true;
    } catch {
      // still down
    }
    await sleep(Math.min(delay, Math.max(0, deadline - Date.now())));
    delay = Math.min(delay * 2, 15_000);
  }
  return false;
}

/** Calls the context API. A 4xx is an answer and throws at once; a 5xx, a non-JSON body or a network
 * error is retried with backoff, then thrown as a ContextError naming the call. */
export async function call<T>(path: string, init?: RequestInit, attempts = 3): Promise<T> {
  const what = `${init?.method ?? "GET"} ${path}`;
  const delay = Number(process.env.AUTONOME_CONTEXT_RETRY_MS ?? 500);
  let failure = "";
  let waited = false;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    if (attempt > 1) await sleep(delay * 2 ** (attempt - 2));
    let res: Response;
    let text: string;
    try {
      res = await fetch(`${CONTEXT_URL}${path}`, {
        ...init,
        headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      });
      text = await res.text();
    } catch (e) {
      failure = `context API unreachable on ${what}: ${String(e)}`;
      // Unreachable usually means restarting: wait for it once, then pick up the same call.
      if (!waited && (await waitForContext())) {
        waited = true;
        attempt--;
      }
      continue;
    }
    let body: (T & { error?: string }) | undefined;
    if (res.headers.get("content-type")?.includes("application/json")) {
      try {
        body = JSON.parse(text);
      } catch {
        body = undefined;
      }
    }
    if (res.status < 500 && body !== undefined) {
      if (!res.ok) throw new Error(`${what}: ${res.status} ${body.error ?? ""}`);
      return body;
    }
    failure = `context API ${res.status} on ${what}: ${text.trim().split("\n")[0].slice(0, 200)}`;
  }
  throw new ContextError(failure);
}

export const context = {
  health: () => call<{ status: string }>("/health"),
  claim: (project: string) =>
    call<{ task: Task | null; rehearse: Plan | null; soundcheck: Plan | null }>(
      `/api/projects/${project}/tasks/claim`, { method: "POST", body: "{}" }),
  soundcheck: (planId: number, r: SoundcheckResult) =>
    call<{ outcome: "done" | "fixing" | "blocked" }>(`/api/plans/${planId}/soundcheck`, {
      method: "POST", body: JSON.stringify(r) }),
  rehearsal: (planId: number, r: Rehearsal) =>
    call<{ plan_status: string }>(`/api/plans/${planId}/rehearsal`, { method: "POST", body: JSON.stringify(r) }),
  transition: (taskId: number, to: string, run?: Run) =>
    call<{ status: string }>(`/api/tasks/${taskId}/transition`, {
      method: "POST",
      body: JSON.stringify({ to, run }),
    }),
  untunedWaves: (project: string) => call<Plan[]>(`/api/projects/${project}/waves/untuned`),
  waveTuned: (waveId: number, summary: string) =>
    call<{ ok: boolean }>(`/api/waves/${waveId}/tuned`, { method: "POST", body: JSON.stringify({ summary }) }),
  briefing: (project: string, taskId: number) =>
    call<{ text: string }>(`/api/projects/${project}/briefing?task_id=${taskId}`),
  gateConfig: (project: string) => call<GateConfig>(`/api/projects/${project}/gate-config`),
  features: (project: string) => call<Feature[]>(`/api/projects/${project}/features`),
  enqueue: (project: string, task: { title: string; spec: string; source: string; feature?: string }) =>
    call<{ task_id: number; plan_id: number; plan_status?: string; duplicate: boolean }>(
      `/api/projects/${project}/tasks`, { method: "POST", body: JSON.stringify(task) }),
};

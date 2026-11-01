// autonome doctor: what the runner needs, checked before it runs instead of found out mid-wave.
import { spawnSync } from "node:child_process";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { CONTEXT_URL, call, roleModel, type GateConfig } from "./context.js";
import { git, mainCheckout } from "./git.js";
import { findJevKey, secretsFile, sessionEnv } from "./secrets.js";
import { pingJev } from "./triage.js";

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
  /** Passes, but worth saying out loud. */
  warn?: boolean;
}

const OLLAMA_URL = process.env.OLLAMA_URL ?? "http://127.0.0.1:11434";
const EMBED_MODEL = process.env.AUTONOME_EMBED_MODEL ?? "nomic-embed-text";

async function reach(url: string): Promise<Response | null> {
  try {
    return await fetch(url, { signal: AbortSignal.timeout(5000) });
  } catch {
    return null;
  }
}

/** The slice of the SDK's query the doctor uses; tests inject a fake. */
export interface ModelsQuery {
  supportedModels(): Promise<{ value: string; supportsAutoMode?: boolean }[]>;
  close(): void;
}
export type ModelsQueryFactory = (options: { model: string }) => ModelsQuery;

/** A streaming-input query that never gets a prompt: only asks the SDK which models it knows, then closes. */
export function modelsQueryVia(sdkQuery: typeof query): ModelsQueryFactory {
  return (options) => sdkQuery({
    prompt: (async function* () { await new Promise<never>(() => {}); })(),
    options,
  }) as unknown as ModelsQuery;
}
const sdkModelsQuery = modelsQueryVia(query);

async function autoModeCheck(cfg: GateConfig["config"], seam: ModelsQueryFactory): Promise<{ ok: boolean; detail: string }> {
  const role = roleModel(cfg, "implementer");
  const wanted = [role.model, ...(role.fallback ? [role.fallback] : [])];
  let q: ModelsQuery | undefined;
  try {
    q = seam({ model: role.model });
    const known = await q.supportedModels();
    const unsupported = wanted.filter((m) => !known.find((k) => k.value === m)?.supportsAutoMode);
    if (unsupported.length === 0) {
      return { ok: true, detail: `${wanted.join(" and ")} ${wanted.length > 1 ? "support" : "supports"} auto mode` };
    }
    return { ok: false, detail: `${unsupported.join(" and ")} ${unsupported.length > 1 ? "do" : "does"} not support auto mode ` +
      "→ change the implementer's model (or its fallback) in the gate config, or set permissions back to allowlist" };
  } catch (e) {
    return { ok: false, detail: `could not ask the SDK which models support auto mode (${e instanceof Error ? e.message : String(e)})` +
      " → fix the SDK or set permissions back to allowlist" };
  } finally {
    try { q?.close(); } catch { /* closing is best effort */ }
  }
}

export async function doctor(project: string, repo: string, seams: { query?: ModelsQueryFactory } = {}): Promise<Check[]> {
  const checks: Check[] = [];
  const add = (name: string, ok: boolean, detail: string) => checks.push({ name, ok, detail });

  const health = await reach(`${CONTEXT_URL}/health`);
  add("context", !!health?.ok, health?.ok ? `${CONTEXT_URL} answers`
    : `${CONTEXT_URL} does not answer → docker compose up -d --wait (in the autonome repo)`);

  const tags = await reach(`${OLLAMA_URL}/api/tags`);
  if (!tags?.ok) add("ollama", false, `${OLLAMA_URL} does not answer → docker compose up -d --wait ollama (native mode: open Ollama on the host)`);
  else {
    const { models = [] } = (await tags.json()) as { models?: { name: string }[] };
    const pulled = models.some((m) => m.name.split(":")[0] === EMBED_MODEL);
    add("ollama", pulled, pulled ? `${EMBED_MODEL} is pulled`
      : `${EMBED_MODEL} is not pulled → docker compose run --rm ollama-pull`);
  }

  const got = health?.ok ? await call<GateConfig>(`/api/projects/${project}/gate-config`).catch(() => null) : null;
  if (health?.ok && !got) add("gates", false, "the gate config could not be read from the context");
  if (got) {
    // A greenfield project may have no gates yet but approved plans that bring them: they join the
    // config when their setup task merges, and that task must be able to play first.
    const configured = got.config.gates;
    const pending = got.pending ?? [];
    const all = [...configured, ...pending];
    const head = (g: { run: string }) => g.run.trim().replace(/^!\s*/, "").split(/\s+/)[0];
    // A pending gate's repo-local command (./gradlew, scripts/x.sh) is what its setup task creates;
    // only tools it expects on the host can be missing now.
    const local = (cmd: string) => cmd.includes("/") && !cmd.startsWith("/");
    const commands = [...configured.map(head), ...pending.map(head).filter((cmd) => !local(cmd))];
    const missing = [...new Set(commands)]
      .filter((cmd) => cmd && spawnSync("sh", ["-c", `command -v ${JSON.stringify(cmd)}`], { cwd: repo, env: sessionEnv() }).status !== 0);
    add("gates", all.length > 0 && missing.length === 0,
      all.length === 0 ? "no gates configured → add them in the UI (Config)"
        : missing.length ? `not found in ${repo}: ${missing.join(", ")} → install them or fix the gate command`
          : configured.length ? `${configured.map((g) => g.name).join(", ")}: commands found` +
              (pending.length ? `; ${pending.length} more pending from approved plans` : "")
            : `no gates yet; ${pending.length} pending from approved plans (${pending.map((g) => g.name).join(", ")}),` +
              " they join when their setup task merges");
  }

  const triage = got?.config.triage;
  if (triage?.model === "jev-latest") {
    const found = findJevKey();
    const fallback = triage.fallback === "off" ? "deviations stay proposed" : `${triage.fallback} judges instead`;
    if (found.key) {
      // A one-question call: a wrong endpoint or a revoked key shows here, not as a silent fallback.
      try {
        add("triage", true, `Jev key found (${found.source}); Jev answers (${await pingJev(found.key)})`);
      } catch (e) {
        add("triage", true, `warning: Jev key found (${found.source}) but Jev did not answer ` +
          `(${e instanceof Error ? e.message : String(e)}); ${fallback}`);
        checks[checks.length - 1].warn = true;
      }
    } else {
      add("triage", true, `warning: no Jev key in TYPESAFE_API_KEY or ${secretsFile()}` +
        (found.warning ? ` (${found.warning})` : "") + `; ${fallback}`);
      checks[checks.length - 1].warn = true;
    }
  }

  if (got?.config.permissions === "auto") {
    const auto = await autoModeCheck(got.config, seams.query ?? sdkModelsQuery);
    add("permissions", auto.ok, `permissions auto: ${auto.detail}`);
  }

  const main = mainCheckout(repo);
  const dirty = main ? git(main, "status", "--porcelain") : "";
  add("main", !dirty, dirty ? `uncommitted changes at ${main} → commit or stash them first` : "clean");
  return checks;
}

export function report(checks: Check[]): string {
  return checks.map((c) => `${c.ok ? "✔" : "✘"} ${c.name.padEnd(8)} ${c.detail}`).join("\n");
}

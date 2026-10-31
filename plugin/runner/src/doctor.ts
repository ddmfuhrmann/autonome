// autonome doctor: what the maestro needs, checked before it plays instead of found out mid-wave.
import { spawnSync } from "node:child_process";
import { CONTEXT_URL, call, type GateConfig } from "./context.js";
import { git, mainCheckout } from "./git.js";

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
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

export async function doctor(project: string, repo: string): Promise<Check[]> {
  const checks: Check[] = [];
  const add = (name: string, ok: boolean, detail: string) => checks.push({ name, ok, detail });

  const health = await reach(`${CONTEXT_URL}/health`);
  add("context", !!health?.ok, health?.ok ? `${CONTEXT_URL} answers`
    : `${CONTEXT_URL} does not answer → docker compose up -d --wait (in the autonome repo)`);

  const tags = await reach(`${OLLAMA_URL}/api/tags`);
  if (!tags?.ok) add("ollama", false, `${OLLAMA_URL} does not answer → docker compose up -d --wait ollama`);
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
    // A pending gate's repo-local command (./gradlew, ./scripts/x) is what its setup task creates;
    // only tools it expects on the host can be missing now.
    const local = (cmd: string) => cmd.startsWith("./");
    const commands = [...configured.map(head), ...pending.map(head).filter((cmd) => !local(cmd))];
    const missing = [...new Set(commands)]
      .filter((cmd) => cmd && spawnSync("sh", ["-c", `command -v ${JSON.stringify(cmd)}`], { cwd: repo }).status !== 0);
    add("gates", all.length > 0 && missing.length === 0,
      all.length === 0 ? "no gates configured → add them in the UI (Config)"
        : missing.length ? `not found in ${repo}: ${missing.join(", ")} → install them or fix the gate command`
          : configured.length ? `${configured.map((g) => g.name).join(", ")}: commands found` +
              (pending.length ? `; ${pending.length} more pending from approved plans` : "")
            : `no gates yet; ${pending.length} pending from approved plans (${pending.map((g) => g.name).join(", ")}),` +
              " they join when their setup task merges");
  }

  const main = mainCheckout(repo);
  const dirty = main ? git(main, "status", "--porcelain") : "";
  add("main", !dirty, dirty ? `uncommitted changes at ${main} → commit or stash them first` : "clean");
  return checks;
}

export function report(checks: Check[]): string {
  return checks.map((c) => `${c.ok ? "✔" : "✘"} ${c.name.padEnd(8)} ${c.detail}`).join("\n");
}

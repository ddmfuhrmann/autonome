// Nothing the runner starts stays running: a command with a ceiling runs in its own process group and
// dies whole, and the processes a session leaves behind are found by their ancestry and killed.
import { spawn, spawnSync } from "node:child_process";
import type { Options } from "@anthropic-ai/claude-agent-sdk";

/** pid -> ppid of every process on the machine. */
function processTable(): Map<number, number> {
  const r = spawnSync("ps", ["-axo", "pid=,ppid="], { encoding: "utf8" });
  const table = new Map<number, number>();
  for (const line of (r.stdout ?? "").split("\n")) {
    const [pid, ppid] = line.trim().split(/\s+/).map(Number);
    if (Number.isInteger(pid) && Number.isInteger(ppid)) table.set(pid, ppid);
  }
  return table;
}

/** The roots and every descendant of them, never the runner itself. */
export function treeOf(roots: number[]): number[] {
  const table = processTable();
  const found = new Set(roots.filter((p) => p !== process.pid && table.has(p)));
  for (let grew = true; grew; ) {
    grew = false;
    for (const [pid, ppid] of table) {
      if (!found.has(pid) && found.has(ppid) && pid !== process.pid) { found.add(pid); grew = true; }
    }
  }
  return [...found];
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function signalAll(pids: number[], sig: NodeJS.Signals): void {
  for (const pid of pids) {
    try { process.kill(pid, sig); } catch { /* already gone */ }
  }
}

function alive(pid: number): boolean {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

/** SIGTERM, then SIGKILL after `graceMs` to whatever is still alive. */
export async function killAll(pids: number[], graceMs = 5000): Promise<void> {
  signalAll(pids, "SIGTERM");
  const deadline = Date.now() + graceMs;
  while (pids.some(alive) && Date.now() < deadline) await sleep(50);
  signalAll(pids.filter(alive), "SIGKILL");
}

export type SpawnSession = NonNullable<Options["spawnClaudeCodeProcess"]>;

/** Runs `fn` with a spawner for the SDK's process (`spawnClaudeCodeProcess`): the process starts in its
 * own group and, when `fn` ends, whatever is left of it and of everything below it is killed. Only that
 * tree is touched; the runner's other children (docs, handoff writer, gates) are not part of it. */
export async function withSessionProcess<T>(
  fn: (spawnSession: SpawnSession) => Promise<T>, log?: (line: string) => void, graceMs = 5000,
): Promise<T> {
  const roots = new Set<number>();
  const tracked = new Set<number>();
  // Followed while the session lives: once its process exits, what it left is reparented and out of the tree.
  const mark = () => { for (const pid of treeOf([...roots])) tracked.add(pid); };
  const watcher = setInterval(mark, 250);
  const spawnSession: SpawnSession = ({ command, args, cwd, env, signal }) => {
    const child = spawn(command, args, { cwd, env: env as NodeJS.ProcessEnv, signal, detached: true, stdio: ["pipe", "pipe", "ignore"] });
    if (child.pid) { roots.add(child.pid); tracked.add(child.pid); }
    return child;
  };
  try {
    return await fn(spawnSession);
  } finally {
    clearInterval(watcher);
    mark();
    const left = [...tracked].filter((pid) => pid !== process.pid && alive(pid));
    for (const pid of roots) { try { process.kill(-pid, "SIGTERM"); } catch { /* group already gone */ } }
    if (left.length > 0) {
      await killAll(left, graceMs);
      for (const pid of roots) { try { process.kill(-pid, "SIGKILL"); } catch { /* group already gone */ } }
      log?.(`  killed ${left.length} leftover process${left.length === 1 ? "" : "es"}`);
    }
  }
}

// One runner per project on this machine. A second `run` would claim tasks next to the first, and while
// the lock is held by a live runner, a task left `running` is never taken for one a dead runner left.
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

export class RunLockedError extends Error {
  constructor(project: string, pid: number) {
    super(`another runner is already running for ${project} (pid ${pid}); stop it first`);
  }
}

export function lockPath(home: string, project: string): string {
  return join(home, "state", `run-${project}.lock`);
}

function holder(path: string): number | null {
  try {
    const pid = Number((JSON.parse(readFileSync(path, "utf8")) as { pid?: unknown }).pid);
    return Number.isInteger(pid) && pid > 0 ? pid : null;
  } catch {
    return null; // unreadable or half written: no one holds it
  }
}

function alive(pid: number): boolean {
  try { process.kill(pid, 0); return true; } catch (e) { return (e as NodeJS.ErrnoException).code === "EPERM"; }
}

/** Takes the project's run lock, or throws `RunLockedError` while a live process holds it. A lock left by a
 * runner that died is taken over. Returns the release, which only removes a lock this process holds. */
export function acquireRunLock(home: string, project: string, pid = process.pid): () => void {
  const path = lockPath(home, project);
  mkdirSync(dirname(path), { recursive: true });
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      writeFileSync(path, JSON.stringify({ pid, at: new Date().toISOString() }), { flag: "wx" });
      return () => { if (holder(path) === pid) rmSync(path, { force: true }); };
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
      const other = holder(path);
      if (other !== null && other !== pid && alive(other)) throw new RunLockedError(project, other);
      rmSync(path, { force: true });
    }
  }
  throw new Error(`could not take the run lock at ${path}`);
}

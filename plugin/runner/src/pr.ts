// Pull requests through `gh`, for merge.mode: pr. Only the runner's own process calls it; sessions never see gh.
import { spawnSync } from "node:child_process";
import type { Run } from "./context.js";
import { sessionEnv } from "./secrets.js";

/** Runs `gh` with these arguments in `cwd`; tests replace it with a fake. */
export type Gh = (args: string[], cwd: string) => { ok: boolean; out: string };

export const gh: Gh = (args, cwd) => {
  const r = spawnSync("gh", args, { cwd, encoding: "utf8", env: sessionEnv(), timeout: 60_000 });
  if (r.error) return { ok: false, out: (r.error as NodeJS.ErrnoException).code === "ENOENT" ? "gh is not installed" : String(r.error) };
  return { ok: r.status === 0, out: `${r.stdout ?? ""}${r.stderr ?? ""}`.trim() };
};

/** gh failed (missing, not logged in, no network): infrastructure, never the task's fault. */
export class PrError extends Error {}

export interface OpenedPr {
  url: string;
  /** Who merges: `auto` only when GitHub accepted the auto-merge, else the human does. */
  mode: "manual" | "auto";
  /** Set when `auto` was asked for but could not be turned on. */
  warning?: string;
}

/** Opens a pull request from `branch` (already pushed) into main; with `auto`, asks GitHub to merge it when CI passes. */
export function openPr(run: Gh, cwd: string, o: { branch: string; title: string; body: string; mode: "manual" | "auto" }): OpenedPr {
  const created = run(["pr", "create", "--base", "main", "--head", o.branch, "--title", o.title, "--body", o.body], cwd);
  const url = created.out.split("\n").map((l) => l.trim()).filter((l) => /^https:\/\/\S+$/.test(l)).at(-1);
  if (!created.ok || !url) throw new PrError(`gh pr create failed: ${created.out.slice(-400) || "no URL printed"}`);
  if (o.mode === "manual") return { url, mode: "manual" };
  const auto = run(["pr", "merge", "--auto", "--rebase", url], cwd);
  // The PR is open already: a refused auto-merge (not allowed on the repo) leaves it for the human instead of opening a second one.
  return auto.ok ? { url, mode: "auto" } : { url, mode: "manual", warning: `could not turn on auto-merge: ${auto.out.slice(-300)}` };
}

/** The run the runner posts when it moves a task to `in_pr` (contracts/runner-context.json: task_in_pr). */
export function prOpenedRun(run: Run, pr: { url: string; mode: "manual" | "auto" }): Run {
  return { ...run, outcome: "pr_opened", pr_url: pr.url, pr_mode: pr.mode };
}

export interface PrState {
  state: "OPEN" | "MERGED" | "CLOSED";
  mergeSha?: string;
}

/** The state of a pull request. Throws PrError when gh or the network fails. */
export function prState(run: Gh, cwd: string, url: string): PrState {
  const r = run(["pr", "view", url, "--json", "state,mergeCommit"], cwd);
  if (!r.ok) throw new PrError(`gh pr view failed: ${r.out.slice(-300)}`);
  try {
    const v = JSON.parse(r.out) as { state?: string; mergeCommit?: { oid?: string } | null };
    if (v.state !== "OPEN" && v.state !== "MERGED" && v.state !== "CLOSED") throw new Error(`state ${String(v.state)}`);
    return { state: v.state, mergeSha: v.mergeCommit?.oid ?? undefined };
  } catch (e) {
    throw new PrError(`gh pr view printed something unexpected: ${String(e)}`);
  }
}

/** Whether gh is installed and logged in. */
export function ghAuth(run: Gh, cwd: string): { ok: boolean; detail: string } {
  const r = run(["auth", "status"], cwd);
  return { ok: r.ok, detail: r.ok ? "gh is logged in" : `${r.out.split("\n")[0] || "gh auth status failed"} → install gh and run gh auth login` };
}

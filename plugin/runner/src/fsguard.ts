// File commands scoped to the task's worktree. A permission rule like Bash(rm:*) cannot say where, so
// conduct sessions get a PreToolUse hook instead: rm, mkdir, mv, cp, chmod and touch are allowed when
// every path they name resolves inside the worktree, and denied with the reason otherwise.
import { existsSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import type { HookCallbackMatcher } from "@anthropic-ai/claude-agent-sdk";

export const SCOPED = new Set(["rm", "mkdir", "mv", "cp", "chmod", "touch"]);

// Anything that would run a second command or write somewhere else: chaining, pipes, substitution,
// redirection, background jobs, variables.
const UNSAFE = /[;&|`<>$\n]|\(\s*\)/;

/** The real path of `p`, or of its closest existing parent plus the rest (for files about to exist). */
function real(p: string): string {
  let head = p;
  const tail: string[] = [];
  while (!existsSync(head) && dirname(head) !== head) {
    tail.unshift(head.slice(dirname(head).length + 1));
    head = dirname(head);
  }
  return resolve(realpathSync(head), ...tail);
}

/** null when the command may run; otherwise why not. Only judges the scoped commands. */
export function judge(command: string, worktree: string): string | null {
  const cmd = command.trim();
  const name = cmd.split(/\s+/)[0];
  if (!SCOPED.has(name)) return `${name} is not a scoped file command`;
  if (UNSAFE.test(cmd)) return `${name}: one command at a time, without ;, &&, |, $(...), redirection or variables`;
  const words = cmd.match(/'[^']*'|"[^"]*"|\S+/g)!.slice(1).map((w) => w.replace(/^(['"])(.*)\1$/, "$2"));
  const paths = words.filter((w) => !w.startsWith("-") && !(name === "chmod" && /^[ugoa]*[+-=][rwxXst]+$|^[0-7]{3,4}$/.test(w)));
  if (!paths.length) return `${name}: no path given`;
  const root = real(worktree);
  for (const p of paths) {
    if (p.startsWith("~")) return `${name}: ${p} is outside the worktree`;
    const target = real(isAbsolute(p) ? p : resolve(worktree, p.replace(/[*?[].*$/, "") || "."));
    const rel = relative(root, target);
    if (rel.startsWith("..") || isAbsolute(rel)) return `${name}: ${p} is outside the worktree`;
    if (rel === "" && name === "rm") return "rm: not the worktree itself";
    if (rel === ".git" || rel.startsWith(`.git${sep}`)) return `${name}: ${p} is git's own data`;
  }
  return null;
}

/** The hook for query(): decides the scoped file commands and leaves every other tool call alone. */
export function scopedFileCommands(worktree: string): HookCallbackMatcher {
  return {
    matcher: "Bash",
    hooks: [async (input) => {
      const command = String(((input as { tool_input?: { command?: unknown } }).tool_input?.command) ?? "");
      if (!SCOPED.has(command.trim().split(/\s+/)[0])) return {};
      const why = judge(command, worktree);
      return { hookSpecificOutput: why
        ? { hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: why }
        : { hookEventName: "PreToolUse", permissionDecision: "allow", permissionDecisionReason: "inside the task's worktree" } };
    }],
  };
}

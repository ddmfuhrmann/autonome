// Bash commands a prefix rule cannot judge, decided by a PreToolUse hook in runner sessions:
// - rm, mkdir, mv, cp, chmod and touch run when every path they name is inside the task's worktree;
// - bash, sh and python3 run a script the rules already allow (a gate's command);
// - find runs anywhere without -delete or -exec (reading a jar's metadata in ~/.gradle);
// - VAR=value or env in front of an allowed command is judged by the command itself.
// Anything else is left to the permission rules, which deny what they do not list.
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

const words = (cmd: string) => cmd.match(/'[^']*'|"[^"]*"|\S+/g)!.map((w) => w.replace(/^(['"])(.*)\1$/, "$2"));

function inside(p: string, worktree: string): string | null {
  if (p.startsWith("~")) return "outside the worktree";
  const rel = relative(real(worktree), real(isAbsolute(p) ? p : resolve(worktree, p.replace(/[*?[].*$/, "") || ".")));
  if (rel.startsWith("..") || isAbsolute(rel)) return "outside the worktree";
  if (rel === ".git" || rel.startsWith(`.git${sep}`)) return "git's own data";
  return null;
}

/** The command without leading `env` and VAR=value assignments. */
export function stripEnv(command: string): string {
  return command.trim().replace(/^env\s+/, "").replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|"[^"]*"|\S*)\s+)+/, "");
}

/** The command prefixes of Bash(...) permission rules, e.g. "./gradlew" from "Bash(./gradlew:*)". */
export function bashPrefixes(tools: string[]): string[] {
  return tools.map((t) => /^Bash\((.+):\*\)$/.exec(t)?.[1]).filter((p): p is string => !!p);
}

const FIND_WRITES = /\s-(delete|exec|execdir|ok|okdir|fprint0?|fprintf|fls)\b/;
const INTERPRETERS = new Set(["bash", "sh", "python3"]);

/** "allow", or why the command is denied, or null to leave it to the permission rules. */
export function decide(command: string, worktree: string, allowed: string[] = []): "allow" | string | null {
  // Chained, piped, redirected or substituted: the permission rules decide (and deny).
  if (/[;&|`<>\n]|\$\(/.test(command)) return null;
  const cmd = stripEnv(command);
  const [name, ...args] = words(cmd);
  if (SCOPED.has(name)) return judge(cmd, worktree) ?? "allow";
  if (name === "find") return FIND_WRITES.test(` ${cmd}`) ? "find: -delete and -exec are not allowed; read only" : "allow";
  if (INTERPRETERS.has(name)) {
    // Only a script the rules already run (a gate's command): any other script could hold anything.
    const script = args.find((a) => !a.startsWith("-"))?.replace(/^\.\//, "");
    if (!script || args.includes("-c") || inside(script, worktree)) return null;
    return allowed.some((p) => p.replace(/^\.\//, "") === script) ? "allow" : null;
  }
  // VAR=value in front of a command the rules already allow.
  if (cmd !== command.trim() && allowed.some((p) => cmd === p || cmd.startsWith(`${p} `))) return "allow";
  return null;
}

/** null when the command may run; otherwise why not. Only judges the scoped commands. */
export function judge(command: string, worktree: string): string | null {
  const cmd = command.trim();
  const name = cmd.split(/\s+/)[0];
  if (!SCOPED.has(name)) return `${name} is not a scoped file command`;
  if (UNSAFE.test(cmd)) return `${name}: one command at a time, without ;, &&, |, $(...), redirection or variables`;
  const words_ = words(cmd).slice(1);
  const paths = words_.filter((w) => !w.startsWith("-") && !(name === "chmod" && /^[ugoa]*[+-=][rwxXst]+$|^[0-7]{3,4}$/.test(w)));
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

/** The hook for query(): decides what `decide` knows and leaves every other call to the rules. */
export function scopedFileCommands(worktree: string, allowed: string[] = []): HookCallbackMatcher {
  return {
    matcher: "Bash",
    hooks: [async (input) => {
      const command = String(((input as { tool_input?: { command?: unknown } }).tool_input?.command) ?? "");
      const verdict = decide(command, worktree, allowed);
      if (verdict === null) return {};
      return { hookSpecificOutput: verdict === "allow"
        ? { hookEventName: "PreToolUse", permissionDecision: "allow", permissionDecisionReason: "scoped to the task's worktree" }
        : { hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: verdict } };
    }],
  };
}

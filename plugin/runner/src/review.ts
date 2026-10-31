// The reviewer: an agent that did not write the code reads the task's diff against its plan and the
// project's conventions, before anything merges. Gates check that the code passes; this checks that
// it is the code the plan asked for.
import { consult } from "./agents.js";
import type { Finding, ReviewResult, RoleModel, Task } from "./context.js";
import { git } from "./git.js";

const REVIEWER_PROMPT = `You review one task of the autonome process before it merges. You did not write it.
The gates (tests, linters) already passed; do not re-check what they check. Read the diff, the task,
its plan and the conventions for its layers (all in the briefing below), and the code around the diff
when you need it. Judge it against all three, not only the plan: a thin plan does not excuse thin code.

Two kinds of finding:
- "diverges": the code does something the task, the plan or a convention says it must not: scope the
  task did not ask for or the plan rules out, an acceptance criterion not actually met, a bug a reader
  can see (wrong condition, missing error path, a constraint or security rule broken).
- "missing": something the code ignores: an edge the acceptance names (malformed or boundary input,
  control characters, a dependency down, an exhausted retry, a repeated or concurrent call, injection,
  open redirect, SSRF) or a convention of its layers it does not follow. Set \`convention\` to the
  convention's title when one requires it.

Tests: flag a test that only executes code without asserting behaviour (it exists for coverage), or
that is shaped to pass rather than to check, as "missing" for the behaviour it should have checked.

Severity: high blocks the merge. A "diverges" finding that is real and matters is high. A "missing"
finding is high when a convention or the task's acceptance requires it, however unlikely the case
(a raw 500 that "almost never happens" still breaks a "no raw 500" convention); otherwise medium or
low, an advisory note the process learns from. Always fill \`convention\` when one applies: a missing
finding against a named convention blocks. Be concrete: file, line or function, what is wrong and why.
No style nits a linter would catch, no praise. verdict "fail" only if there is at least one high finding.`;

const SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["pass", "fail"] },
    findings: {
      type: "array",
      items: {
        type: "object",
        properties: {
          kind: { type: "string", enum: ["diverges", "missing"] },
          severity: { type: "string", enum: ["high", "medium", "low"] },
          convention: { type: "string", description: "title of the convention that requires it, if any" },
          file: { type: "string" },
          issue: { type: "string" },
        },
        required: ["kind", "severity", "issue"],
      },
    },
  },
  required: ["verdict", "findings"],
} as const;

export async function review(task: Task, briefing: string, worktree: string, base: string, who: RoleModel) {
  const diff = git(worktree, "diff", "--cached", base).slice(0, 80_000);
  const { answer, cost } = await consult<ReviewResult>({
    cwd: worktree,
    ...who,
    role: REVIEWER_PROMPT,
    schema: SCHEMA,
    maxTurns: 30,
    prompt: [
      `Review task #${task.id} of project ${task.project}. The implementer's briefing:`,
      briefing,
      `## Diff against ${base.slice(0, 10)}\n\`\`\`diff\n${diff}\n\`\`\``,
    ].join("\n\n"),
  });
  const findings = enforceConventions(answer.findings, conventionTitles(briefing));
  // The verdict follows the findings, whatever the agent wrote in it.
  const verdict = findings.some((f) => f.severity === "high") ? "fail" : "pass";
  return { result: { verdict, findings } as ReviewResult, cost };
}

/** The titles under the briefing's "Conventions for these layers" section. */
export function conventionTitles(briefing: string): string[] {
  const section = briefing.split(/^## /m).find((s) => s.startsWith("Conventions for these layers")) ?? "";
  return [...section.matchAll(/^### (.+)$/gm)].map((m) => m[1].trim());
}

/** A convention is a rule, not a suggestion: code that ignores an active one does not merge, whatever
 * severity the reviewer gave it. A cheap reviewer finds these but tends to rate them low (shortlink v2:
 * the raw 500 an active convention forbids was rated low and merged). */
export function enforceConventions(findings: Finding[], titles: string[]): Finding[] {
  return findings.map((f) => {
    const named = f.convention || titles.find((t) => f.issue.toLowerCase().includes(t.toLowerCase()));
    if (f.kind === "missing" && named && f.severity !== "high") {
      return { ...f, convention: named, severity: "high", issue: `${f.issue} (blocking: required by the convention "${named}")` };
    }
    return f;
  });
}

export function findingsForEncore(r: ReviewResult): string {
  return r.findings.filter((f) => f.severity === "high")
    .map((f) => `- ${f.file ? `${f.file}: ` : ""}${f.issue}`).join("\n");
}

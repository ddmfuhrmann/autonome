// The reviewer: an agent that did not write the code reads the task's diff against its plan and the
// project's conventions, before anything merges. Gates check that the code passes; this checks that
// it is the code the plan asked for.
import { consult } from "./agents.js";
import type { ReviewResult, Task } from "./context.js";
import { git } from "./git.js";

const REVIEWER_PROMPT = `You review one task of the autonome process before it merges. You did not write it.
The gates (tests, linters) already passed; do not re-check what they check. Read the diff, the task,
its plan and the conventions for its layers (all in the briefing below), and the code around the diff
when you need it. Look for:

- scope: changes the task did not ask for, or anything the plan lists as out of scope;
- the task's acceptance criteria that the diff does not actually meet (a test that asserts nothing,
  is skipped, or was shaped to pass rather than to check the behaviour);
- convention or constraint violations (security, personal data, tenant isolation, money);
- bugs a reader can see: wrong condition, missing error path, broken edge case.

Severity: high blocks the merge (any of the above that is real and matters); medium and low are notes.
Be concrete: file, line or function, what is wrong and why. No style nits that a linter would catch,
no praise. verdict "fail" only if there is at least one high finding.`;

const SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["pass", "fail"] },
    findings: {
      type: "array",
      items: {
        type: "object",
        properties: {
          severity: { type: "string", enum: ["high", "medium", "low"] },
          file: { type: "string" },
          issue: { type: "string" },
        },
        required: ["severity", "issue"],
      },
    },
  },
  required: ["verdict", "findings"],
} as const;

export async function review(task: Task, briefing: string, worktree: string, base: string, model?: string) {
  const diff = git(worktree, "diff", "--cached", base).slice(0, 80_000);
  const { answer, cost } = await consult<ReviewResult>({
    cwd: worktree,
    model,
    role: REVIEWER_PROMPT,
    schema: SCHEMA,
    maxTurns: 30,
    prompt: [
      `Review task #${task.id} of project ${task.project}. The implementer's briefing:`,
      briefing,
      `## Diff against ${base.slice(0, 10)}\n\`\`\`diff\n${diff}\n\`\`\``,
    ].join("\n\n"),
  });
  // The verdict follows the findings, whatever the agent wrote in it.
  const verdict = answer.findings.some((f) => f.severity === "high") ? "fail" : "pass";
  return { result: { verdict, findings: answer.findings } as ReviewResult, cost };
}

export function findingsForEncore(r: ReviewResult): string {
  return r.findings.filter((f) => f.severity === "high")
    .map((f) => `- ${f.file ? `${f.file}: ` : ""}${f.issue}`).join("\n");
}

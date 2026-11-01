// The reviewer: an agent that did not write the code reads the task's diff against its plan and the
// project's conventions, before anything merges. Gates check that the code passes; this checks that
// it is the code the plan asked for.
import { consult } from "./agents.js";
import type { Finding, ReviewResult, RoleModel, Run, Task, Triage } from "./context.js";
import { git } from "./git.js";
import { downgradeAdvisory } from "./triage.js";

const REVIEWER_PROMPT = `You review one task of the autonome process before it merges. You did not write it.
The gates (tests, linters, type checks, coverage) already ran and passed; their results are in the
briefing under "Gates". Do not re-check what they check. Read the diff, the task, its plan and the
conventions for its layers (all in the briefing below), and the code around the diff when you need it.
Judge it against all three, not only the plan: a thin plan does not excuse thin code.

You only read. Bash runs git (log, diff, show, status) and read-only commands such as cat, ls, find
and grep; anything that runs project code is denied (uv, pytest, python, node, the gates' commands,
loops over files), so do not try it. When you want to know how the code behaves on an input (an edge,
a probe, a case you would run by hand), read the tests for it; if no test covers it, report a
"missing" finding that asks for a test of exactly that case, with the input and the expected result,
instead of guessing. Never report that you could not run something.

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

export interface GateOutcome { gate: string; passed: boolean; output: string }

/** The gates the runner just ran, for the reviewer: facts from data, so it does not try to run them
 * again (its Bash is read-only, and every denied try costs a turn). Each gate's last output line
 * carries its number when it has one (coverage, test counts). */
export function gatesSection(outcomes: GateOutcome[] | undefined): string {
  if (!outcomes?.length) return "";
  const last = (o: string) => o.trim().split("\n").filter((l) => l.trim()).pop()?.trim().slice(0, 200) ?? "";
  return "## Gates (already run on this diff)\n" + outcomes.map((o) =>
    `- ${o.gate}: ${o.passed ? "passed" : "failed"}${last(o.output) ? `: ${last(o.output)}` : ""}`).join("\n");
}

/** For a review after a retry: the earlier findings, to check instead of starting over. A reviewer
 * that raises new medium points every round never converges and asks for opposite things. */
export function earlierFindingsSection(previous: Finding[] | undefined): string {
  if (!previous?.length) return "";
  return "## Earlier findings (your previous round)\n" + previous.map((f) =>
    `- ${f.severity} ${f.kind}${f.file ? ` in ${f.file}` : ""}: ${f.issue}`).join("\n") +
    "\n\nThe implementer was sent back with these. For each, check whether the diff resolves it now; report " +
    "it again only if it is still there, with the same severity unless it got worse. Beyond those, report only " +
    "new high findings: no new medium or low ones in this round, and never ask to undo what an earlier finding asked for.";
}

export async function review(task: Task, briefing: string, worktree: string, base: string, who: RoleModel & { language?: string },
  onMessage?: (msg: unknown) => void, previous?: Finding[],
  triage?: { cfg?: Triage | null; key?: string; ask?: (f: Finding, key: string) => Promise<number> }) {
  const diff = git(worktree, "diff", "--cached", base).slice(0, 80_000);
  const { answer, cost } = await consult<ReviewResult>({
    cwd: worktree,
    onMessage,
    ...who,
    role: REVIEWER_PROMPT,
    schema: SCHEMA,
    maxTurns: 30,
    prompt: [
      `Review task #${task.id} of project ${task.project}. The implementer's briefing:`,
      briefing,
      `## Diff against ${base.slice(0, 10)}\n\`\`\`diff\n${diff}\n\`\`\``,
      earlierFindingsSection(previous),
    ].filter(Boolean).join("\n\n"),
  });
  const enforced = enforceConventions(answer.findings, conventionTitles(briefing));
  const findings = await downgradeAdvisory(enforced, triage?.cfg, triage?.key, triage?.ask);
  // The verdict follows the findings, whatever the agent wrote in it.
  const verdict = findings.some((f) => f.severity === "high") ? "fail" : "pass";
  return { result: { verdict, findings } as ReviewResult, cost };
}

const findingKey = (f: Finding) => [f.kind ?? "", f.file ?? "", f.issue.trim().toLowerCase()].join("|");

/** What became of each finding once the run is over. A finding of an earlier round is `repeated` when
 * the next round reported it again, else `resolved`; those of the last round are `merged` (left as a
 * loose end) or `blocked` (the task blocked on them). */
export function settleOutcomes(rounds: ReviewResult[], final?: "merged" | "blocked"): void {
  rounds.forEach((r, i) => {
    const next = rounds[i + 1];
    const again = next ? new Set(next.findings.map(findingKey)) : null;
    for (const f of r.findings) f.outcome = again ? (again.has(findingKey(f)) ? "repeated" : "resolved") : final;
  });
}

/** Keeps a round on the run: `reviews` has every round, `review` stays the last for older readers. */
export function recordRound(run: Pick<Run, "review" | "reviews" | "review_rounds">, result: ReviewResult, round: number): void {
  run.review = result;
  (run.reviews ??= []).push(result);
  run.review_rounds = round;
}

/** Settles the run's rounds when it ends. The last round's findings are `blocked` only when the task
 * blocked on the review; a block from anything else (gates, a merge conflict) leaves them unmarked. */
export function settleRun(run: Pick<Run, "reviews">, last?: "merged" | "blocked"): void {
  if (run.reviews) settleOutcomes(run.reviews, last);
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

export function findingsForRetry(r: ReviewResult, severity: Finding["severity"] = "high"): string {
  return r.findings.filter((f) => f.severity === severity)
    .map((f) => `- ${f.file ? `${f.file}: ` : ""}${f.issue}`).join("\n");
}

const FIX_OR_EXPLAIN = "Fix each one, or, when it is not a problem in the code (a deviation the plan allowed, a " +
  "check you could not run), say so in your final summary so the handoff records it.";

/** What goes back to the implementer after a review, or null when nothing does. High findings block;
 * medium ones go back too while review rounds remain, so debt is paid while the session has the
 * context, and become loose ends at merge once rounds run out. Low ones never go back. */
export function retryMessage(r: ReviewResult, roundsLeft: boolean): string | null {
  const high = findingsForRetry(r, "high");
  const medium = findingsForRetry(r, "medium");
  if (high) {
    return "An independent reviewer read your diff and found blocking issues. Fix them, then end your turn:\n" + high
      + (medium ? `\n\nAlso worth fixing now. ${FIX_OR_EXPLAIN}\n${medium}` : "");
  }
  if (medium && roundsLeft) {
    return `An independent reviewer passed your diff with findings worth fixing now. ${FIX_OR_EXPLAIN} ` +
      "Then end your turn:\n" + medium;
  }
  return null;
}

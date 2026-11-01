import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { allowedTools, rehearsalPrompt, summary } from "./conduct.js";
import { parseConfigs, table } from "./bench.js";
import { roleModel, sdkModel } from "./context.js";
import { commitMessage, fastForwardMain, git, stageWork, strayFiles } from "./git.js";

function repo(): string {
  const dir = mkdtempSync(join(tmpdir(), "autonome-runner-"));
  git(dir, "init", "-q", "-b", "main");
  git(dir, "config", "user.email", "t@t");
  git(dir, "config", "user.name", "t");
  writeFileSync(join(dir, "a.txt"), "a\n");
  git(dir, "add", ".");
  git(dir, "commit", "-qm", "init");
  return dir;
}

function branchWithCommit(dir: string, name: string): void {
  const wt = join(dir, "..", `${name}-wt-${Date.now()}`);
  git(dir, "worktree", "add", "-q", "-b", name, wt, "main");
  writeFileSync(join(wt, "b.txt"), "b\n");
  git(wt, "add", ".");
  git(wt, "commit", "-qm", "b");
}

test("commit subjects lowercase a plain first word and keep acronyms and names as they are", () => {
  const subject = (title: string) => commitMessage({ id: 1, feature: "links", body: { title, plan_id: 1 } }).split("\n")[0];
  assert.equal(subject("Count clicks per link"), "feat(links): count clicks per link");
  assert.equal(subject("POST /links with URL validation"), "feat(links): POST /links with URL validation");
  assert.equal(subject("SQLite connection, schema and test fixture"), "feat(links): SQLite connection, schema and test fixture");
  assert.equal(subject("GET /links/{code}/stats"), "feat(links): GET /links/{code}/stats");
});

test("a title whose type word is followed by punctuation keeps the word", () => {
  const subject = (title: string) => commitMessage({ id: 1, feature: "triage", body: { title, plan_id: 1 } }).split("\n")[0];
  assert.equal(subject("Docs, README, CLAUDE.md and version"), "docs(triage): docs, README, CLAUDE.md and version");
  assert.equal(subject("Docs"), "docs(triage): docs");
  assert.equal(subject("Docs: README and version"), "docs(triage): README and version");
});

test("commit message follows conventional commits with plan and task trailers", () => {
  const msg = commitMessage({ id: 7, feature: "wallet", body: { title: "Fix: Rounding of fees", plan_id: 3 } });
  assert.match(msg, /^fix\(wallet\): rounding of fees\n\nPlan: #3\nTask: #7\n/);
  assert.match(commitMessage({ id: 1, feature: null, body: { title: "Add signup form", plan_id: 1 } }), /^feat: add signup form/);
});

test("main is fast-forwarded in place when its checkout is clean", () => {
  const dir = repo();
  branchWithCommit(dir, "task");
  assert.deepEqual(fastForwardMain(dir, "task"), { ok: true });
  assert.equal(git(dir, "rev-parse", "main"), git(dir, "rev-parse", "task"));
  assert.equal(git(dir, "status", "--porcelain"), "");
});

test("uncommitted work on the main checkout is never touched", () => {
  const dir = repo();
  branchWithCommit(dir, "task");
  writeFileSync(join(dir, "a.txt"), "local edit\n");
  const r = fastForwardMain(dir, "task");
  assert.equal(r.ok, false);
  assert.match(r.reason!, /uncommitted changes/);
});

test("main is updated by ref when it is not checked out anywhere", () => {
  const dir = repo();
  git(dir, "checkout", "-q", "-b", "other");
  branchWithCommit(dir, "task");
  assert.deepEqual(fastForwardMain(dir, "task"), { ok: true });
  assert.equal(git(dir, "rev-parse", "main"), git(dir, "rev-parse", "task"));
});

test("a musician may run the gates' commands and nothing else in the shell", () => {
  const tools = allowedTools({
    gates: [{ name: "lint", run: "./mvnw -q checkstyle:check" }, { name: "neg", run: "! grep -q TODO x" }],
    allowed_commands: ["pnpm"],
  });
  assert.ok(tools.includes("Bash(./mvnw:*)") && tools.includes("Bash(pnpm:*)") && tools.includes("Bash(autonome-gates:*)"));
  assert.ok(tools.includes("mcp__plugin_autonome_autonome__gate_config_get") && tools.includes("mcp__autonome__gate_config_get"));
  assert.ok(tools.includes("Bash(sed -n:*)") && !tools.some((t) => t.startsWith("Bash(sed:") || t.startsWith("Bash(python") || t.startsWith("Bash(cat")));
  assert.ok(!tools.some((t) => t === "Bash" || t.startsWith("Bash(git commit") || t.startsWith("Bash(git push")));
});

test("a feature's docs drift when work landed after its doc, or it has work and no doc", async () => {
  const { drifted } = await import("./cues.js");
  assert.deepEqual(drifted([
    { slug: "fresh", handoffs: 2, documented: true, last_work_at: "2026-10-01", doc_updated_at: "2026-10-02" },
    { slug: "stale", handoffs: 2, documented: true, last_work_at: "2026-10-03", doc_updated_at: "2026-10-02" },
    { slug: "undocumented", handoffs: 1, documented: false, last_work_at: "2026-10-01", doc_updated_at: null },
    { slug: "idle", handoffs: 0, documented: false, last_work_at: null, doc_updated_at: null },
  ]), ["stale", "undocumented"]);
});

test("a generated file staged in one round drops out once it is ignored (task #23 of shortlink)", () => {
  const dir = repo();
  writeFileSync(join(dir, "app.py"), "print(1)\n");
  writeFileSync(join(dir, ".coverage"), "binary data");
  stageWork(dir);
  assert.match(git(dir, "diff", "--cached", "--name-only"), /\.coverage/);
  writeFileSync(join(dir, ".gitignore"), ".coverage\n");
  writeFileSync(join(dir, ".coverage"), "rewritten by the next test run");
  stageWork(dir);
  assert.deepEqual(git(dir, "diff", "--cached", "--name-only").split("\n"), [".gitignore", "app.py"]);
});

test("stray files: ignored-but-tracked and unasked-for binaries are flagged, asked-for ones are not", () => {
  const dir = repo();
  const base = git(dir, "rev-parse", "HEAD");
  writeFileSync(join(dir, ".coverage"), Buffer.from([0, 1, 2, 0, 255]));
  writeFileSync(join(dir, "logo.png"), Buffer.from([137, 80, 78, 71, 0, 0]));
  writeFileSync(join(dir, "app.py"), "print(1)\n");
  stageWork(dir);
  assert.deepEqual(strayFiles(dir, base, "Add the logo.png to the header"),
    [".coverage: a new binary file the task does not mention"]);
  git(dir, "add", "-f", ".coverage");
  writeFileSync(join(dir, ".gitignore"), ".coverage\n");
  git(dir, "add", ".gitignore");
  assert.deepEqual(strayFiles(dir, base, "Add the logo.png to the header"), [
    ".coverage: tracked although .gitignore excludes it",
    ".coverage: a new binary file the task does not mention",
  ]);
  stageWork(dir);
  assert.deepEqual(strayFiles(dir, base, "Add the logo.png to the header"), []);
});

test("the rehearsal sees the gates a plan must pass, with their commands", () => {
  const text = rehearsalPrompt(
    { id: 16, project: "shortlink", status: "approved", feature: "links",
      body: { title: "Links", tasks: [{ title: "Routes", spec: "app imports db" }], rehearsal: { verdict: "ready" } } },
    "shortlink", [{ name: "imports", run: "uv run lint-imports" }, { name: "pytest", run: "uv run pytest -q" }]);
  assert.match(text, /## Gates it must pass\n- imports: `uv run lint-imports`\n- pytest: `uv run pytest -q`/);
  assert.match(text, /app imports db/);
  assert.doesNotMatch(text, /"rehearsal"/);
});

test("the conductor's summary counts outcomes and totals time and tokens", () => {
  const text = summary([
    { id: 24, title: "SQLite schema", outcome: "merged", review_rounds: 1, duration_s: 312, tokens: 41000 },
    { id: 25, title: "POST /links", outcome: "blocked", review_rounds: 3, duration_s: 95, tokens: 9000,
      reason: "imports: the same failure repeated\nmore" },
  ]);
  assert.match(text, /^summary: 1 merged, 1 blocked, 0 held · 6m47s · 50000 tokens/);
  assert.match(text, /#25 blocked .*review 3  POST \/links\n {6}imports: the same failure repeated$/);
  assert.equal(summary([]), "");
});

test("each role runs on its own model and effort, and the command line overrides them all", () => {
  const cfg = { gates: [], encore: { max: 3, stop_on_repeat: true }, budget: { task_tokens: 1, task_minutes: 1, plan_tokens: 1 },
    review: { enabled: true, max_rounds: 2 }, soundcheck: null,
    models: { musician: { model: "sonnet", effort: "medium" as const }, reviewer: { model: "sonnet", effort: "high" as const } } };
  assert.deepEqual(roleModel(cfg, "reviewer"), { model: "sonnet", effort: "high" });
  assert.deepEqual(roleModel(cfg, "musician", { model: "opus" }), { model: "opus", effort: "medium" });
  assert.deepEqual(roleModel(cfg, "musician", { effort: "low" }), { model: "sonnet", effort: "low" });
  assert.deepEqual(roleModel({ ...cfg, models: undefined }, "tune"), { model: "sonnet", effort: "medium" });
});

test("a role's fallback model reaches the SDK, unless it is the model itself", () => {
  const cfg = { gates: [], encore: { max: 3, stop_on_repeat: true }, budget: { task_tokens: 1, task_minutes: 1, plan_tokens: 1 },
    review: { enabled: true, max_rounds: 2 }, soundcheck: null,
    models: { musician: { model: "sonnet", effort: "medium" as const, fallback: "haiku" } } };
  const musician = roleModel(cfg, "musician");
  assert.deepEqual(musician, { model: "sonnet", effort: "medium", fallback: "haiku" });
  assert.deepEqual(sdkModel(musician), { model: "sonnet", effort: "medium", fallbackModel: "haiku" });
  assert.deepEqual(roleModel(cfg, "musician", { model: "haiku" }), { model: "haiku", effort: "medium" });
  assert.deepEqual(sdkModel({ model: "sonnet", effort: "low" }), { model: "sonnet", effort: "low" });
});

test("bench configs parse as model:effort and the table totals the cost", () => {
  assert.deepEqual(parseConfigs("haiku:low, sonnet"), [{ model: "haiku", effort: "low" }, { model: "sonnet", effort: "medium" }]);
  const text = table([{ role: "reviewer", case: "reviewer/post-links", who: { model: "haiku", effort: "low" }, score: 0.67,
    cost: 0.0412, seconds: 31, note: "found control chars, raw 500" }]);
  assert.match(text, /reviewer\/post-links +haiku:low +0\.67 +\$ 0\.041 +31s  found control chars, raw 500\ntotal \$0\.04$/);
});

test("a build wrapper's jar is not a stray binary, in the repo root or a subproject", () => {
  const dir = repo();
  const base = git(dir, "rev-parse", "HEAD");
  for (const path of ["gradle/wrapper/gradle-wrapper.jar", "apps/landing/.mvn/wrapper/maven-wrapper.jar", "lib/blob.bin"]) {
    mkdirSync(join(dir, path, ".."), { recursive: true });
    writeFileSync(join(dir, path), Buffer.from([0, 1, 2, 0, 255]));
  }
  stageWork(dir);
  assert.deepEqual(strayFiles(dir, base, "Set up the Gradle build with its wrapper"),
    ["lib/blob.bin: a new binary file the task does not mention"]);
});

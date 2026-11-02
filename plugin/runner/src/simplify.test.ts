import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";
import { git } from "./git.js";
import { runSimplify, simplifyOptions, type SimplifySession } from "./simplify.js";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });

const schedule = { name: "simp", kind: "simplify" as const, every_hours: 24 };
let repo: string;
let stateDir: string;
let enqueued: { title: string; spec: string; source: string }[];

function commit(file: string, text: string): string {
  mkdirSync(join(repo, file, ".."), { recursive: true });
  writeFileSync(join(repo, file), text);
  git(repo, "add", "-A");
  git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", `edit ${file}`);
  return git(repo, "rev-parse", "HEAD");
}

beforeEach(() => {
  repo = mkdtempSync(join(tmpdir(), "simplify-repo-"));
  stateDir = mkdtempSync(join(tmpdir(), "simplify-state-"));
  git(repo, "init", "-b", "main");
  commit("src/a.ts", "export const a = 1;\n");
  enqueued = [];
  let n = 0;
  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/gate-config")) return json({ config: { gates: [] } });
    if (url.endsWith("/tasks")) { enqueued.push(JSON.parse(String(init?.body))); return json({ task_id: ++n, plan_id: 1, duplicate: false }); }
    return json({});
  };
});

const opts = () => ({ project: "p", stateDir, log: (l: string) => logs.push(l) });
let logs: string[] = [];
beforeEach(() => { logs = []; });
const proposal = (i: number) => ({ title: `Simplify ${i}`, files: ["src/a.ts"], evidence: `src/a.ts:${i} repeats itself`, change: `merge the copies ${i}` });
const answering = (n: number): SimplifySession => async () => ({ proposals: Array.from({ length: n }, (_, i) => proposal(i + 1)) });
const sha = () => JSON.parse(readFileSync(join(stateDir, "simplify-p.json"), "utf8")).sha;

test("two proposals become two tasks with the evidence in the spec", async () => {
  const out = await runSimplify(schedule, repo, opts(), answering(2));
  assert.equal(out, "queued #1, #2");
  assert.equal(enqueued.length, 2);
  assert.equal(enqueued[0].source, "schedule:simp");
  assert.match(enqueued[0].spec, /src\/a\.ts:1 repeats itself/);
  assert.match(enqueued[0].spec, /merge the copies 1/);
  assert.match(enqueued[0].spec, /Keep behaviour; tests must still pass/);
  assert.equal(sha(), git(repo, "rev-parse", "HEAD"));
});

test("more proposals than max_tasks: only the first ones are queued", async () => {
  await runSimplify({ ...schedule, max_tasks: 3 }, repo, opts(), answering(5));
  assert.deepEqual(enqueued.map((t) => t.title), ["Simplify 1", "Simplify 2", "Simplify 3"]);
});

test("an answer outside the schema, or a failed session, queues nothing and keeps the sha", async () => {
  for (const bad of [async () => ({ proposals: [{ title: "x" }] }), async () => "nope", async () => { throw new Error("boom"); }]) {
    logs = [];
    assert.equal(await runSimplify(schedule, repo, opts(), bad as SimplifySession), "failed");
    assert.match(logs[0], /^error: /);
  }
  assert.deepEqual(enqueued, []);
  assert.equal(existsSync(join(stateDir, "simplify-p.json")), false);
});

test("nothing changed since the stored sha: no session is opened", async () => {
  await runSimplify(schedule, repo, opts(), answering(1));
  let sessions = 0;
  const out = await runSimplify(schedule, repo, opts(), async () => { sessions++; return { proposals: [] }; });
  assert.equal(out, "nothing changed");
  // tests and docs changing is not a change either
  commit("src/a.test.ts", "// test\n");
  commit("docs/x.md", "# doc\n");
  assert.equal(await runSimplify(schedule, repo, opts(), async () => { sessions++; return { proposals: [] }; }), "nothing changed");
  assert.equal(sessions, 0);
});

test("only the code changed since the stored sha goes to the session", async () => {
  await runSimplify(schedule, repo, opts(), answering(1));
  commit("src/b.ts", "export const b = 2;\n");
  let prompt = "";
  await runSimplify(schedule, repo, opts(), async (req) => { prompt = req.prompt; return { proposals: [] }; });
  assert.match(prompt, /src\/b\.ts/);
  assert.doesNotMatch(prompt, /src\/a\.ts/);
});

test("the session's options only read: dontAsk, no Bash, no Edit or Write", () => {
  const o = simplifyOptions({ gates: [] } as never, repo, "p");
  assert.equal(o.permissionMode, "dontAsk");
  assert.deepEqual(o.allowedTools, ["Read", "Grep", "Glob"]);
  assert.doesNotMatch(JSON.stringify(o.allowedTools), /Bash|Edit|Write/);
  assert.ok((o.disallowedTools as string[]).includes("Read(~/.autonome/**)"));
  assert.ok((o.disallowedTools as string[]).includes("Bash"));
  // project settings could add allow rules: none are loaded
  assert.equal(o.settingSources, undefined);
});

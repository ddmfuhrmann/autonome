import assert from "node:assert/strict";
import { mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import { doctor, report } from "./doctor.js";
import { git } from "./git.js";

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });

function repo(): string {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "autonome-doctor-")));
  git(dir, "init", "-q", "-b", "main");
  git(dir, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init");
  return dir;
}

test("doctor passes when everything the maestro needs is there", async () => {
  globalThis.fetch = async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/gate-config")) return json({ config: { gates: [{ name: "lint", run: "git status" }, { name: "neg", run: "! grep -q TODO x" }] } });
    return json({ status: "ok" });
  };
  const checks = await doctor("p", repo());
  assert.ok(checks.every((c) => c.ok), report(checks));
});

test("doctor names each problem with a fix: model not pulled, gate tool missing, dirty main", async () => {
  globalThis.fetch = async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/api/tags")) return json({ models: [] });
    if (url.endsWith("/gate-config")) return json({ config: { gates: [{ name: "imports", run: "lint-imports-nope --strict" }] } });
    return json({ status: "ok" });
  };
  const dir = repo();
  writeFileSync(join(dir, "wip.txt"), "x");
  const text = report((await doctor("p", dir)).filter((c) => !c.ok));
  assert.match(text, /✘ ollama .*not pulled → docker compose run --rm ollama-pull/);
  assert.match(text, /✘ gates .*lint-imports-nope → install/);
  assert.match(text, /✘ main .*uncommitted changes/);
});

test("doctor says when the context is down instead of failing on it", async () => {
  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  const checks = await doctor("p", repo());
  assert.deepEqual(checks.map((c) => [c.name, c.ok]), [["context", false], ["ollama", false], ["main", true]]);
});

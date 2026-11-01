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

test("doctor passes when everything the runner needs is there", async () => {
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

function contextWith(gateConfig: unknown, jev: "answers" | "404" = "answers") {
  globalThis.fetch = async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/api/tags")) return json({ models: [{ name: "nomic-embed-text:latest" }] });
    if (url.endsWith("/gate-config")) return json(gateConfig);
    if (url.endsWith("/v1/systemone")) {
      return jev === "404" ? new Response("not found", { status: 404 })
        : json({ model: "jev-1.14.0", answers: { billing: { type: "noul", noul: 0.97 } } });
    }
    return json({ status: "ok" });
  };
}

const gatesCheck = async (gateConfig: unknown) => {
  contextWith(gateConfig);
  return (await doctor("p", repo())).find((c) => c.name === "gates")!;
};

test("greenfield: no gates configured and none pending from a plan still fails", async () => {
  const c = await gatesCheck({ version: null, config: { gates: [] }, pending: [] });
  assert.equal(c.ok, false);
  assert.match(c.detail, /no gates configured → add them in the UI/);
});

test("greenfield: gates pending from approved plans let the run start, and are named", async () => {
  const c = await gatesCheck({ version: null, config: { gates: [] }, pending: [
    { name: "ruff-format", run: "git status", task_id: 202, plan_id: 185 },
    { name: "pytest", run: "git status", task_id: 202, plan_id: 185 }] });
  assert.equal(c.ok, true);
  assert.equal(c.detail, "no gates yet; 2 pending from approved plans (ruff-format, pytest), they join when their setup task merges");
});

test("greenfield: a pending gate whose command is not installed fails like a configured one", async () => {
  const c = await gatesCheck({ version: null, config: { gates: [] }, pending: [
    { name: "mypy", run: "nope-not-installed run mypy", task_id: 202, plan_id: 185 }] });
  assert.equal(c.ok, false);
  assert.match(c.detail, /not found in .*: nope-not-installed → install them/);
});

test("greenfield: a pending gate's repo-local command is created by its setup task, so it is not required yet", async () => {
  const c = await gatesCheck({ version: null, config: { gates: [] }, pending: [
    { name: "build", run: "./gradlew build", task_id: 291, plan_id: 278 },
    { name: "secrets", run: "scripts/check-secrets.sh", task_id: 392, plan_id: 390 }] });
  assert.equal(c.ok, true, c.detail);
});

test("jev-latest without a key warns, does not fail, and never prints a key", async () => {
  const saved = { key: process.env.TYPESAFE_API_KEY, home: process.env.AUTONOME_HOME };
  delete process.env.TYPESAFE_API_KEY;
  process.env.AUTONOME_HOME = mkdtempSync(join(tmpdir(), "autonome-doctor-home-"));
  try {
    contextWith({ version: 1, config: { gates: [{ name: "lint", run: "git status" }],
      triage: { model: "jev-latest", fallback: "haiku", reject_at: 0.7, max_rounds: 2 } }, pending: [] });
    const checks = await doctor("p", repo());
    assert.ok(checks.every((c) => c.ok), report(checks));
    const t = checks.find((c) => c.name === "triage")!;
    assert.equal(t.warn, true);
    assert.match(t.detail, /no Jev key.*haiku judges instead/);

    process.env.TYPESAFE_API_KEY = "sk-very-secret";
    const found = (await doctor("p", repo())).find((c) => c.name === "triage")!;
    assert.equal(found.ok, true);
    assert.equal(found.warn, undefined);
    assert.match(found.detail, /Jev key found \(environment\); Jev answers \(jev-1\.14\.0\)/);
    assert.ok(!found.detail.includes("sk-very-secret"));

    // A wrong endpoint or a revoked key warns here instead of falling back in silence.
    contextWith({ version: 1, config: { gates: [{ name: "lint", run: "git status" }],
      triage: { model: "jev-latest", fallback: "haiku", reject_at: 0.7, max_rounds: 2 } }, pending: [] }, "404");
    const broken = (await doctor("p", repo())).find((c) => c.name === "triage")!;
    assert.equal(broken.ok, true);
    assert.equal(broken.warn, true);
    assert.match(broken.detail, /Jev did not answer \(jev: HTTP 404\); haiku judges instead/);
    assert.ok(!broken.detail.includes("sk-very-secret"));
  } finally {
    for (const [k, v] of [["TYPESAFE_API_KEY", saved.key], ["AUTONOME_HOME", saved.home]] as const) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
  }
});

test("a configured gate's repo-local command must exist", async () => {
  const c = await gatesCheck({ version: 1, config: { gates: [{ name: "build", run: "./gradlew build" }] }, pending: [] });
  assert.equal(c.ok, false);
  assert.match(c.detail, /not found in .*: \.\/gradlew/);
});

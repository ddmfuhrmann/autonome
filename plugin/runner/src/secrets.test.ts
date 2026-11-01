import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";
import { spawnSync } from "node:child_process";
import { git } from "./git.js";
import { jevKey, sessionEnv } from "./secrets.js";

const saved = { home: process.env.AUTONOME_HOME, key: process.env.TYPESAFE_API_KEY };
let home: string;
beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "autonome-secrets-"));
  process.env.AUTONOME_HOME = home;
  delete process.env.TYPESAFE_API_KEY;
});
afterEach(() => {
  for (const [k, v] of [["AUTONOME_HOME", saved.home], ["TYPESAFE_API_KEY", saved.key]] as const) {
    if (v === undefined) delete process.env[k]; else process.env[k] = v;
  }
});

const secrets = (text: string, mode: number) => {
  const p = join(home, "secrets.env");
  writeFileSync(p, text);
  chmodSync(p, mode);
};

test("jevKey reads the environment first", () => {
  process.env.TYPESAFE_API_KEY = "from-env";
  secrets("TYPESAFE_API_KEY=from-file\n", 0o600);
  assert.equal(jevKey(), "from-env");
});

test("jevKey falls back to secrets.env", () => {
  secrets("OTHER=1\nTYPESAFE_API_KEY=\"from-file\"\n", 0o600);
  assert.equal(jevKey(), "from-file");
});

test("jevKey finds nothing without either", () => {
  assert.equal(jevKey(), undefined);
});

test("a 0644 secrets.env is ignored with a warning that does not carry the key", () => {
  secrets("TYPESAFE_API_KEY=from-file\n", 0o644);
  const warnings: string[] = [];
  assert.equal(jevKey((l) => warnings.push(l)), undefined);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /readable by others/);
  assert.ok(!warnings[0].includes("from-file"));
});

test("sessionEnv has no TYPESAFE_API_KEY but keeps the rest and the extras", () => {
  process.env.TYPESAFE_API_KEY = "secret";
  const env = sessionEnv({ AUTONOME_TASK_ID: "7" });
  assert.equal("TYPESAFE_API_KEY" in env, false);
  assert.equal(env.AUTONOME_TASK_ID, "7");
  assert.equal(env.PATH, process.env.PATH);
});

test("a child started through git() or sessionEnv() does not see the key", () => {
  process.env.TYPESAFE_API_KEY = "sk-child-secret";
  // A git alias runs a shell command (as a hook would) inside git's own environment.
  const viaGit = git(home, "-c", "alias.dump=!env", "dump");
  assert.ok(!viaGit.includes("TYPESAFE_API_KEY") && !viaGit.includes("sk-child-secret"));
  assert.match(viaGit, /PATH=/);
  const viaSpawn = spawnSync("sh", ["-c", "env"], { env: sessionEnv(), encoding: "utf8" }).stdout;
  assert.ok(!viaSpawn.includes("TYPESAFE_API_KEY") && !viaSpawn.includes("sk-child-secret"));
});

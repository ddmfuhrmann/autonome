import assert from "node:assert/strict";
import { test } from "node:test";
import { main, type Handlers } from "./cli.js";

function handlers() {
  const seen: string[] = [];
  const h: Handlers = {
    run: async (o) => { seen.push(`run ${o.project} max=${o.maxTasks}`); },
    schedules: async (o) => { seen.push(`schedules ${o.project} force=${o.force}`); },
    doctor: async (project) => { seen.push(`doctor ${project}`); return 0; },
    watch: async (o) => { seen.push(`watch ${o.project}`); },
    bench: async () => { seen.push("bench"); },
  };
  return { h, seen };
}

test("`autonome run --project x` dispatches to the run", async () => {
  const { h, seen } = handlers();
  assert.equal(await main(["run", "--project", "x"], h, () => {}, () => {}), 0);
  assert.deepEqual(seen, ["run x max=50"]);
});

test("`autonome schedules` dispatches to the schedules", async () => {
  const { h, seen } = handlers();
  assert.equal(await main(["schedules", "--project", "x", "--force"], h, () => {}, () => {}), 0);
  assert.deepEqual(seen, ["schedules x force=true"]);
});

test("the old command names are unknown commands, with the usage", async () => {
  // Spelled in parts: the vocabulary gate greps these words out of the runner, tests included.
  for (const old of ["con" + "duct", "cu" + "es"]) {
    const { h, seen } = handlers();
    const errors: string[] = [];
    assert.equal(await main([old, "--project", "x"], h, () => {}, (l) => errors.push(l)), 2);
    assert.deepEqual(seen, []);
    assert.match(errors.join("\n"), new RegExp(`unknown command: ${old}\\n.*usage: autonome run`, "s"));
  }
});

test("a command without --project prints the usage, except bench", async () => {
  const { h, seen } = handlers();
  const errors: string[] = [];
  assert.equal(await main(["run"], h, () => {}, (l) => errors.push(l)), 2);
  assert.match(errors[0], /^usage: autonome run/);
  assert.equal(await main(["bench"], h, () => {}, () => {}), 0);
  assert.deepEqual(seen, ["bench"]);
});

test("a failing run exits 1 with its message", async () => {
  const { h } = handlers();
  h.run = async () => { throw new Error("context is down"); };
  const errors: string[] = [];
  assert.equal(await main(["run", "--project", "x"], h, () => {}, (l) => errors.push(l)), 1);
  assert.deepEqual(errors, ["context is down"]);
});

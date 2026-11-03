import assert from "node:assert/strict";
import { test } from "node:test";
import type { Finding, Triage } from "./context.js";
import { advisory, advisoryLine, downgradeAdvisory, DOWNGRADE_NOTE, RAISE_NOTE, raiseRequired, raisedLine, requiredByAcceptance } from "./triage.js";
import { enforceConventions } from "./review.js";

const cfg: Triage = { model: "jev-latest", fallback: "haiku", reject_at: 0.7, max_rounds: 2 };
const silence = () => { console.error = () => undefined; };

// #228: a high finding whose own text says it is optional
const f228: Finding = { kind: "diverges", severity: "high", file: "app/links.py",
  issue: "Advisory only, not blocking: the redirect could also log the code." };

test("a high finding whose text says advisory is downgraded and the downgrade is recorded", async () => {
  const sent: Finding[] = [];
  const [out] = await downgradeAdvisory([f228], cfg, "k", async (f) => { sent.push(f); return 0.9; });
  assert.equal(sent.length, 1);
  assert.equal(out.severity, "medium");
  assert.equal(out.downgraded, true);
  assert.equal(out.issue, f228.issue + DOWNGRADE_NOTE);
  assert.equal(out.advisory, 0.9);
});

test("a kept high finding still records Jev's answer, and a failed check records the failure", async () => {
  silence();
  const [kept] = await downgradeAdvisory([f228], cfg, "k", async () => 0.12);
  assert.equal(kept.severity, "high");
  assert.equal(kept.advisory, 0.12);
  assert.equal(kept.downgraded, undefined);
  const [failed] = await downgradeAdvisory([f228], cfg, "k", async () => { throw new Error("jev: HTTP 500"); });
  assert.equal(failed.advisory, undefined);
  assert.equal(failed.advisory_error, "jev: HTTP 500");
});

test("the advisory line says what was checked, or why it was skipped", () => {
  const high: Finding = { kind: "missing", severity: "high", issue: "x" };
  assert.equal(advisoryLine([high, { ...high, severity: "medium", downgraded: true, advisory: 0.81 }, { ...high, advisory_error: "boom" }]),
    "triage advisory: 2 high findings checked (0.81 downgraded, failed)");
  assert.equal(advisoryLine([{ ...high, advisory: 0.05 }]), "triage advisory: 1 high finding checked (0.05)");
  assert.equal(advisoryLine([high]), null);
  assert.equal(advisoryLine([high], "no Jev key"), "triage advisory: skipped, no Jev key");
  assert.equal(advisoryLine([{ ...high, severity: "low" }], "no Jev key"), null);
});

test("below reject_at, or a non-high finding, stays as it was and is not sent for low ones", async () => {
  const [kept] = await downgradeAdvisory([f228], cfg, "k", async () => 0.69);
  assert.equal(kept.severity, "high");
  const asked: string[] = [];
  const [medium] = await downgradeAdvisory([{ ...f228, severity: "medium" }], cfg, "k", async (f) => { asked.push(f.issue); return 1; });
  assert.equal(medium.severity, "medium");
  assert.deepEqual(asked, []);
});

test("a convention-required finding is never sent and stays high", async () => {
  const raised = enforceConventions(
    [{ kind: "missing", severity: "low", issue: "Advisory: handle control characters in the code" }],
    ["Handle control characters"],
  );
  const named: Finding = { kind: "missing", severity: "high", convention: "No raw 500", issue: "optional, not blocking" };
  const asked: string[] = [];
  const out = await downgradeAdvisory([...raised, named], cfg, "k", async (f) => { asked.push(f.issue); return 1; });
  assert.deepEqual(asked, []);
  assert.deepEqual(out.map((f) => f.severity), ["high", "high"]);
});

test("a Jev error leaves severities unchanged", async () => {
  silence();
  const out = await downgradeAdvisory([f228], cfg, "k", async () => { throw new Error("jev: HTTP 500"); });
  assert.equal(out[0].severity, "high");
  assert.equal(out[0].downgraded, undefined);
});

test("never with Haiku, without a key or with triage off", async () => {
  const boom = async (): Promise<number> => { throw new Error("must not be asked"); };
  for (const [c, key] of [[{ ...cfg, model: "haiku" }, "k"], [cfg, undefined], [{ ...cfg, model: "off" }, "k"], [null, "k"]] as const) {
    const [out] = await downgradeAdvisory([f228], c as Triage | null, key, boom);
    assert.equal(out.severity, "high");
  }
});

test("advisory rejects a malformed answer instead of defaulting", async () => {
  await assert.rejects(advisory(f228, "k", async () => ({})), /advisory/);
  await assert.rejects(advisory(f228, "k", async () => ({ advisory: 2 })), /advisory/);
  assert.equal(await advisory(f228, "k", async (state) => {
    assert.deepEqual(state, { finding: { kind: "diverges", file: "app/links.py", issue: f228.issue } });
    return { advisory: 0.8 };
  }), 0.8);
});

// gastos #1336: a requirement of the task, rated low by the reviewer and merged
const f1336: Finding = { kind: "missing", severity: "low", file: "backend/src/test/java/ApiExceptionHandlerIT.java",
  issue: "Still no test of the 500 log. The task requires an ERROR log with the code and no request body." };
const acceptance = ["A 500 logs at ERROR with its code and never the request body"];

test("a low finding the acceptance requires is raised to high, with the task's acceptance sent along", async () => {
  const sent: string[][] = [];
  const [out] = await raiseRequired([f1336], acceptance, cfg, "k", async (_f, a) => { sent.push(a); return 0.92; });
  assert.deepEqual(sent, [acceptance]);
  assert.equal(out.severity, "high");
  assert.equal(out.raised, true);
  assert.equal(out.issue, f1336.issue + RAISE_NOTE);
  assert.equal(out.required, 0.92);
});

test("below reject_at, a high finding or a downgraded one stays as it was", async () => {
  const [kept] = await raiseRequired([f1336], acceptance, cfg, "k", async () => 0.69);
  assert.equal(kept.severity, "low");
  assert.equal(kept.required, 0.69);
  const asked: string[] = [];
  const out = await raiseRequired([f228, { ...f228, severity: "medium", downgraded: true }], acceptance, cfg, "k",
    async (f) => { asked.push(f.issue); return 1; });
  assert.deepEqual(asked, []);
  assert.deepEqual(out.map((f) => f.severity), ["high", "medium"]);
});

test("a failed acceptance check records the failure and keeps the severity", async () => {
  silence();
  const [out] = await raiseRequired([f1336], acceptance, cfg, "k", async () => { throw new Error("jev: HTTP 500"); });
  assert.equal(out.severity, "low");
  assert.equal(out.required_error, "jev: HTTP 500");
});

test("never with Haiku, without a key, with triage off or without acceptance", async () => {
  const boom = async (): Promise<number> => { throw new Error("must not be asked"); };
  for (const [c, key, a] of [[{ ...cfg, model: "haiku" }, "k", acceptance], [cfg, undefined, acceptance],
    [{ ...cfg, model: "off" }, "k", acceptance], [cfg, "k", []]] as const) {
    const [out] = await raiseRequired([f1336], [...a], c as Triage, key, boom);
    assert.equal(out.severity, "low");
  }
});

test("requiredByAcceptance sends the finding and the acceptance, and rejects a malformed answer", async () => {
  let state: unknown;
  const p = await requiredByAcceptance(f1336, acceptance, "k", async (s) => { state = s; return { required: 0.8 }; });
  assert.equal(p, 0.8);
  assert.deepEqual(state, { finding: { kind: "missing", file: f1336.file, issue: f1336.issue }, acceptance });
  await assert.rejects(requiredByAcceptance(f1336, acceptance, "k", async () => ({ required: 2 })), /outside 0..1/);
});

test("the acceptance line counts what was raised and what failed", () => {
  assert.equal(raisedLine([f1336, { ...f1336, required: 0.1 }]), null);
  assert.equal(raisedLine([{ ...f1336, severity: "high", raised: true }, { ...f1336, required_error: "boom" }]),
    "triage acceptance: 1 raised to high, 1 failed");
});

#!/usr/bin/env node
// autonome conduct --project <slug> [--repo <path>] [--model <model>] [--max-tasks <n>] [--push]
// autonome cues    --project <slug> [--repo <path>] [--force]
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { conduct } from "./conduct.js";
import { cues } from "./cues.js";

const { positionals, values } = parseArgs({
  allowPositionals: true,
  options: {
    project: { type: "string" },
    repo: { type: "string", default: process.cwd() },
    model: { type: "string" },
    "max-tasks": { type: "string", default: "50" },
    push: { type: "boolean", default: false },
    force: { type: "boolean", default: false },
  },
});

const command = positionals[0];
if (!["conduct", "cues"].includes(command ?? "") || !values.project) {
  console.error(
    "usage: autonome conduct --project <slug> [--repo <path>] [--model <model>] [--max-tasks <n>] [--push]\n" +
      "       autonome cues --project <slug> [--repo <path>] [--force]",
  );
  process.exit(2);
}

const log = (line: string) => console.log(`[${new Date().toISOString().slice(11, 19)}] ${line}`);
const repo = resolve(values.repo!);
const run =
  command === "cues"
    ? cues({ project: values.project, repo, force: values.force!, log })
    : conduct({ project: values.project, repo, model: values.model, maxTasks: Number(values["max-tasks"]), push: values.push!, log });

run.catch((e) => {
  console.error(String(e instanceof Error ? e.message : e));
  process.exit(1);
});

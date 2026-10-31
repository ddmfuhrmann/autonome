#!/usr/bin/env node
// autonome conduct --project <slug> [--repo <path>] [--model <model>] [--max-tasks <n>] [--push]
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { conduct } from "./conduct.js";

const { positionals, values } = parseArgs({
  allowPositionals: true,
  options: {
    project: { type: "string" },
    repo: { type: "string", default: process.cwd() },
    model: { type: "string" },
    "max-tasks": { type: "string", default: "50" },
    push: { type: "boolean", default: false },
  },
});

if (positionals[0] !== "conduct" || !values.project) {
  console.error("usage: autonome conduct --project <slug> [--repo <path>] [--model <model>] [--max-tasks <n>] [--push]");
  process.exit(2);
}

conduct({
  project: values.project,
  repo: resolve(values.repo!),
  model: values.model,
  maxTasks: Number(values["max-tasks"]),
  push: values.push!,
  log: (line) => console.log(`[${new Date().toISOString().slice(11, 19)}] ${line}`),
}).catch((e) => {
  console.error(String(e instanceof Error ? e.message : e));
  process.exit(1);
});

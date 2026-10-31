#!/usr/bin/env node
// autonome conduct --project <slug> [--repo <path>] [--model <model>] [--max-tasks <n>] [--push] [--watch [--every <s>]]
// autonome cues    --project <slug> [--repo <path>] [--force]
// autonome doctor  --project <slug> [--repo <path>]
// autonome watch   --project <slug> [--every 60 | --once]
// autonome bench   [--configs model:effort,...] [--runs <n>] [--only <case>]
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { conduct } from "./conduct.js";
import type { Effort } from "./context.js";
import { cues } from "./cues.js";
import { bench, parseConfigs, table } from "./bench.js";
import { doctor, report } from "./doctor.js";
import { watch } from "./watch.js";

const { positionals, values } = parseArgs({
  allowPositionals: true,
  options: {
    project: { type: "string" },
    repo: { type: "string", default: process.cwd() },
    model: { type: "string" },
    effort: { type: "string" },
    configs: { type: "string", default: "haiku:low,sonnet:medium,sonnet:high" },
    runs: { type: "string", default: "1" },
    only: { type: "string" },
    "max-tasks": { type: "string", default: "50" },
    push: { type: "boolean", default: false },
    force: { type: "boolean", default: false },
    watch: { type: "boolean", default: false },
    once: { type: "boolean", default: false },
    every: { type: "string", default: "60" },
  },
});

const command = positionals[0];
if (command === "bench") {
  const log = (line: string) => console.log(line);
  const scores = await bench({ configs: parseConfigs(values.configs!), runs: Number(values.runs), only: values.only, log });
  console.log(`\n${table(scores)}`);
  process.exit(0);
}

if (!["conduct", "cues", "doctor", "watch"].includes(command ?? "") || !values.project) {
  console.error(
    "usage: autonome conduct --project <slug> [--repo <path>] [--model <model>] [--effort <level>] [--max-tasks <n>] [--push]\n" +
      "                        [--watch [--every <seconds>]]   keep waiting for approved work\n" +
      "       autonome cues --project <slug> [--repo <path>] [--force]\n" +
      "       autonome doctor --project <slug> [--repo <path>]\n" +
      "       autonome watch --project <slug> [--every 60 | --once]\n" +
      "       autonome bench [--configs haiku:low,sonnet:medium] [--runs <n>] [--only <case>]",
  );
  process.exit(2);
}

const log = (line: string) => console.log(`[${new Date().toISOString().slice(11, 19)}] ${line}`);
const repo = resolve(values.repo!);
if (command === "watch") {
  const time = () => new Date().toTimeString().slice(0, 8);
  await watch({ project: values.project!, every: Number(values.every), once: values.once,
                log: (l) => console.log(values.once ? l : `${time()} ${l}`) });
  process.exit(0);
}

if (command === "doctor") {
  const checks = await doctor(values.project, repo);
  console.log(report(checks));
  process.exit(checks.every((c) => c.ok) ? 0 : 1);
}

const run =
  command === "cues"
    ? cues({ project: values.project, repo, force: values.force!, log })
    : conduct({ project: values.project, repo, model: values.model, effort: values.effort as Effort | undefined, maxTasks: Number(values["max-tasks"]), push: values.push!,
        watch: values.watch ? Number(values.every) : undefined, log });

run.catch((e) => {
  console.error(String(e instanceof Error ? e.message : e));
  process.exit(1);
});

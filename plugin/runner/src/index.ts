#!/usr/bin/env node
// autonome run       --project <slug> [--repo <path>] [--model <model>] [--max-tasks <n>] [--push] [--watch [--every <s>]]
// autonome schedules --project <slug> [--repo <path>] [--force]
// autonome doctor    --project <slug> [--repo <path>]
// autonome watch     --project <slug> [--every 60 | --once]
// autonome bench     [--configs model:effort,...] [--runs <n>] [--only <case>]
import { bench, parseConfigs, table } from "./bench.js";
import { main } from "./cli.js";
import { doctor, report } from "./doctor.js";
import { run } from "./run.js";
import { schedules } from "./schedules.js";
import { watch } from "./watch.js";

const code = await main(process.argv.slice(2), {
  run,
  schedules,
  doctor: async (project, repo) => {
    const checks = await doctor(project, repo);
    console.log(report(checks));
    return checks.every((c) => c.ok) ? 0 : 1;
  },
  watch,
  bench: async (o) => {
    const scores = await bench({ configs: parseConfigs(o.configs), runs: o.runs, only: o.only, log: o.log });
    console.log(`\n${table(scores)}`);
  },
});
process.exit(code);

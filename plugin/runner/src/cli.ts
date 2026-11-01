// The command line: parses the arguments and hands each command to its handler. The handlers are
// injected so tests can see where a command goes without running anything.
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import type { Effort } from "./context.js";
import type { RunOptions } from "./run.js";
import type { ScheduleOptions } from "./schedules.js";

export interface Handlers {
  run: (opts: RunOptions) => Promise<unknown>;
  schedules: (opts: ScheduleOptions) => Promise<unknown>;
  doctor: (project: string, repo: string) => Promise<number>;
  watch: (opts: { project: string; every: number; once: boolean; log: (line: string) => void }) => Promise<unknown>;
  bench: (opts: { configs: string; runs: number; only?: string; log: (line: string) => void }) => Promise<void>;
}

export const USAGE =
  "usage: autonome run --project <slug> [--repo <path>] [--model <model>] [--effort <level>] [--max-tasks <n>] [--push]\n" +
  "                    [--watch [--every <seconds>]]   keep waiting for approved work\n" +
  "       autonome schedules --project <slug> [--repo <path>] [--force]\n" +
  "       autonome doctor --project <slug> [--repo <path>]\n" +
  "       autonome watch --project <slug> [--every 60 | --once]\n" +
  "       autonome bench [--configs haiku:low,sonnet:medium] [--runs <n>] [--only <case>]";

const COMMANDS = ["run", "schedules", "doctor", "watch", "bench"];

/** Runs the command in `argv` (without node and the script); returns the exit code. */
export async function main(argv: string[], handlers: Handlers, out: (line: string) => void = console.log,
  err: (line: string) => void = console.error): Promise<number> {
  const { positionals, values } = parseArgs({
    args: argv,
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
  if (!COMMANDS.includes(command ?? "")) {
    err(`unknown command: ${command ?? "(none)"}\n${USAGE}`);
    return 2;
  }
  if (command === "bench") {
    await handlers.bench({ configs: values.configs!, runs: Number(values.runs), only: values.only, log: out });
    return 0;
  }
  if (!values.project) {
    err(USAGE);
    return 2;
  }

  const log = (line: string) => out(`[${new Date().toISOString().slice(11, 19)}] ${line}`);
  const repo = resolve(values.repo!);
  if (command === "watch") {
    const time = () => new Date().toTimeString().slice(0, 8);
    await handlers.watch({ project: values.project, every: Number(values.every), once: values.once!,
      log: (l) => out(values.once ? l : `${time()} ${l}`) });
    return 0;
  }
  if (command === "doctor") return handlers.doctor(values.project, repo);
  try {
    if (command === "schedules") await handlers.schedules({ project: values.project, repo, force: values.force!, log });
    else {
      await handlers.run({ project: values.project, repo, model: values.model, effort: values.effort as Effort | undefined,
        maxTasks: Number(values["max-tasks"]), push: values.push!, watch: values.watch ? Number(values.every) : undefined, log });
    }
  } catch (e) {
    err(String(e instanceof Error ? e.message : e));
    return 1;
  }
  return 0;
}

// The Jev key reaches the triage call and nothing else: sessions, agents and child processes get an
// environment without it. Nothing here logs or returns the value in a message.
import { existsSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const KEY = "TYPESAFE_API_KEY";

export interface KeyLookup {
  key?: string;
  source?: "environment" | "secrets.env";
  /** A reason the file was not used; never carries the value. */
  warning?: string;
}

export function secretsFile(): string {
  return join(process.env.AUTONOME_HOME ?? join(homedir(), ".autonome"), "secrets.env");
}

function fromFile(path: string): KeyLookup {
  if (!existsSync(path)) return {};
  if (statSync(path).mode & 0o077) {
    return { warning: `${path} is readable by others; ignoring it (chmod 600 ${path})` };
  }
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const m = line.match(new RegExp(`^\\s*(?:export\\s+)?${KEY}\\s*=\\s*(.*?)\\s*$`));
    const value = m?.[1].replace(/^(['"])(.*)\1$/, "$2");
    if (value) return { key: value, source: "secrets.env" };
  }
  return {};
}

/** Where the key is: the environment, else the secrets file. */
export function findJevKey(): KeyLookup {
  const env = process.env[KEY];
  if (env) return { key: env, source: "environment" };
  return fromFile(secretsFile());
}

/** The Jev key, or undefined. A secrets file that others can read is ignored with a warning. */
export function jevKey(warn: (line: string) => void = console.warn): string | undefined {
  const found = findJevKey();
  if (found.warning) warn(found.warning);
  return found.key;
}

/** The environment of anything that is not the triage call: the process's own, minus the Jev key. */
export function sessionEnv(extra: Record<string, string | undefined> = {}): Record<string, string> {
  const { [KEY]: _key, ...rest } = process.env;
  return { ...rest, ...extra } as Record<string, string>;
}

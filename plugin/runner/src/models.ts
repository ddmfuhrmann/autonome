// Every model session the runner starts goes through here. The test script sets AUTONOME_NO_MODELS,
// so a test that forgot its fake fails on the spot instead of spending tokens.
import { query as sdkQuery } from "@anthropic-ai/claude-agent-sdk";

export const NO_MODELS = "AUTONOME_NO_MODELS";

/** Throws when external model calls are off (in tests); `what` names the call in the error. */
export function assertModelsAllowed(what: string): void {
  if (process.env[NO_MODELS]) throw new Error(`${NO_MODELS} is set: ${what} would call a real model; fake it in the test`);
}

export const query: typeof sdkQuery = (args) => {
  assertModelsAllowed("an Agent SDK session");
  return sdkQuery(args);
};

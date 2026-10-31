// The account's usage limit: an outage of the orchestra, not a wrong note. A task that hits it goes
// back to pending; the conductor waits for the reset (with --watch) or stops and says when to resume.

const LIMIT = /hit your (session|usage|weekly|daily) limit|usage limit (reached|exceeded)|rate.?limit(ed)? exceeded/i;

export class UsageLimitError extends Error {}

export function isUsageLimit(text: string): boolean {
  return LIMIT.test(text);
}

/** Milliseconds until the reset the message names ("resets 3:40pm"), plus a minute; 15 minutes when it
 * names none, and never more than 6 hours. */
export function untilReset(text: string, now = new Date()): number {
  const m = /resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)/i.exec(text);
  const fallback = 15 * 60_000;
  if (!m) return fallback;
  let hours = Number(m[1]) % 12;
  if (m[3].toLowerCase() === "pm") hours += 12;
  const reset = new Date(now);
  reset.setHours(hours, Number(m[2] ?? 0), 0, 0);
  if (reset <= now) reset.setDate(reset.getDate() + 1);
  return Math.min(reset.getTime() - now.getTime() + 60_000, 6 * 3_600_000);
}

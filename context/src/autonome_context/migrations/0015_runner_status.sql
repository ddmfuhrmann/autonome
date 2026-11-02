-- What the runner last reported about its environment (today: whether it found the Jev key).
-- Operational state like the pause, so it lives on the project; null until the runner reports.
alter table project add column triage_key boolean, add column runner_seen_at timestamptz;

-- A project's queue can be paused: the runner takes no task while paused_at is set.
-- Operational state, not configuration, so it lives on the project and not in the gate config.
alter table project
    add column paused_at    timestamptz,
    add column paused_by    text,
    add column pause_reason text;

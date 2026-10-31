-- What agent sessions are doing, streamed by the runner while they play: the agent's words, its
-- tool calls and their results, and the maestro's stages. Redacted, capped per task, never indexed.
create table task_event (
    id          bigserial primary key,
    project_id  bigint not null references project (id),
    task_id     bigint references document (id),
    plan_id     bigint references document (id),
    role        text not null,
    kind        text not null,
    text        text not null,
    at          timestamptz not null default now()
);
create index task_event_task_idx on task_event (task_id, id);
create index task_event_plan_idx on task_event (plan_id, id);

-- What each agent session cost, by role: the implementer and the reviewer are in the task's runs, the
-- rest (handoff, docs, preflight, tester, retro, simplifier) only here. Facts, append-only.
create table spend (
    id bigserial primary key,
    project_id bigint not null references project(id),
    role text not null,
    task_id bigint references document(id),
    plan_id bigint references document(id),
    feature text,
    cost_usd numeric(10, 4) not null,
    tokens bigint,
    created_at timestamptz not null default now()
);
create index spend_project_idx on spend (project_id, created_at);

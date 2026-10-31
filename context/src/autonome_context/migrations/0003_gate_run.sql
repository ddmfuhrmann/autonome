-- Facts about gates, recorded by the metronome as they happen. Handoffs and the tune loop read
-- these instead of trusting what an agent says about its own work.
create table gate_run (
    id               bigserial primary key,
    project_id       bigint not null references project (id),
    session_id       text not null,
    task_id          bigint references document (id),
    config_version   int,
    gate             text not null,
    passed           boolean not null,
    duration_ms      int not null,
    signature        text,
    output           text not null default '',
    created_at       timestamptz not null default now()
);

create index gate_run_session_idx on gate_run (session_id);
create index gate_run_project_idx on gate_run (project_id, created_at);

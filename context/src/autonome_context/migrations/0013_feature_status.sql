-- A feature created by an agent is proposed until the human approves it in the UI; usable meanwhile.
-- Features that already exist were in use, so they are active.
alter table feature
    add column status text not null default 'active' check (status in ('active', 'proposed'));

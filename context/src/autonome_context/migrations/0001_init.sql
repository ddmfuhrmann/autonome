create extension if not exists vector;

create table project (
    id          bigserial primary key,
    slug        text not null unique,
    name        text not null,
    created_at  timestamptz not null default now()
);

create table feature (
    id          bigserial primary key,
    project_id  bigint not null references project (id),
    slug        text not null,
    name        text not null,
    unique (project_id, slug)
);

create table layer (
    id          bigserial primary key,
    project_id  bigint not null references project (id),
    slug        text not null,
    unique (project_id, slug)
);

-- Neutral, software-development document types. The process (e.g. the autonome plugin)
-- maps its own vocabulary onto these.
create table document (
    id          bigserial primary key,
    project_id  bigint not null references project (id),
    feature_id  bigint references feature (id),
    type        text not null check (type in
                  ('plan', 'task', 'handoff', 'decision', 'convention', 'gate_config', 'design')),
    status      text,
    created_at  timestamptz not null default now()
);

create table document_layer (
    document_id bigint not null references document (id),
    layer_id    bigint not null references layer (id),
    primary key (document_id, layer_id)
);

-- Append-only: a change is a new version, never an update.
create table document_version (
    id          bigserial primary key,
    document_id bigint not null references document (id),
    version     int not null,
    body        jsonb not null,
    commit_sha  text,
    created_at  timestamptz not null default now(),
    unique (document_id, version)
);

create table link (
    from_id     bigint not null references document (id),
    to_id       bigint not null references document (id),
    kind        text not null,
    primary key (from_id, to_id, kind)
);

-- The raw session, redacted and gzipped. Kept to regenerate handoffs; never indexed.
create table raw_transcript (
    document_id bigint primary key references document (id),
    session_id  text not null,
    content_gz  bytea not null,
    created_at  timestamptz not null default now()
);

create table strategy (
    id          bigserial primary key,
    name        text not null unique,
    params      jsonb not null default '{}',
    embed_model text not null,
    dims        int not null,
    created_at  timestamptz not null default now()
);

-- Untyped vector on purpose: each strategy may use a different model and dimension.
-- Exact scan for now; per-strategy HNSW indexes are a lab variable.
create table chunk (
    id                  bigserial primary key,
    document_version_id bigint not null references document_version (id),
    strategy_id         bigint not null references strategy (id),
    ord                 int not null,
    section             text,
    text                text not null,
    embedding           vector not null,
    tsv                 tsvector generated always as (to_tsvector('simple', text)) stored,
    unique (document_version_id, strategy_id, ord)
);

create index chunk_tsv_idx on chunk using gin (tsv);
create index chunk_strategy_idx on chunk (strategy_id);

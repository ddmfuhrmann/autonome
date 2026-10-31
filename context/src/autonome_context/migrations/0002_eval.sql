-- A question and the documents that should answer it. Synthetic questions are generated from a
-- single document by an LLM, paraphrased so that exact-word matching alone does not find them.
create table eval_question (
    id                     bigserial primary key,
    project_id             bigint not null references project (id),
    question               text not null,
    expected_document_ids  bigint[] not null,
    source                 text not null check (source in ('manual', 'synthetic')),
    created_at             timestamptz not null default now()
);

create table eval_run (
    id           bigserial primary key,
    project_id   bigint not null references project (id),
    strategy_id  bigint not null references strategy (id),
    mode         text not null,
    k            int not null,
    n_questions  int not null,
    recall       float8 not null,
    mrr          float8 not null,
    ndcg         float8 not null,
    details      jsonb not null,
    created_at   timestamptz not null default now()
);

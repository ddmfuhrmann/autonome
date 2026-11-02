-- The feature table is the project's feature catalog: what each feature is, where it lives, and the
-- embedding of its description (for the similarity barrier in store.resolve_feature).
alter table feature
    add column description text,
    add column paths       text[] not null default '{}',
    add column embedding   vector,
    add column embed_model text;

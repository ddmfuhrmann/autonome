-- 0006 moved the misfiled handoffs; the decisions each one records (link kind 'records') stayed in
-- the worktree-named projects. Move them after their handoff, then drop the projects left empty.
do $$
declare
    r record;
    target_feature bigint;
begin
    for r in
        select d.id, h.project_id as target, f.slug as feature
        from document d
        join link l on l.to_id = d.id and l.kind = 'records'
        join document h on h.id = l.from_id and h.type = 'handoff'
        left join feature f on f.id = d.feature_id
        where d.project_id <> h.project_id
    loop
        target_feature := null;
        if r.feature is not null then
            insert into feature (project_id, slug, name) values (r.target, r.feature, r.feature)
                on conflict (project_id, slug) do update set slug = excluded.slug
                returning id into target_feature;
        end if;
        insert into layer (project_id, slug)
            select r.target, l.slug from document_layer dl join layer l on l.id = dl.layer_id
            where dl.document_id = r.id
            on conflict (project_id, slug) do nothing;
        update document_layer dl set layer_id = nl.id
            from layer ol, layer nl
            where dl.document_id = r.id and ol.id = dl.layer_id and nl.project_id = r.target and nl.slug = ol.slug;
        update document set project_id = r.target, feature_id = target_feature where id = r.id;
    end loop;

    delete from feature f where not exists (select 1 from document d where d.feature_id = f.id)
        and f.project_id in (select id from project p where p.slug like 'autonome-task-%');
    delete from layer l where not exists (select 1 from document_layer dl where dl.layer_id = l.id)
        and l.project_id in (select id from project p where p.slug like 'autonome-task-%');
    delete from project p where p.slug like 'autonome-task-%'
        and not exists (select 1 from document d where d.project_id = p.id)
        and not exists (select 1 from feature f where f.project_id = p.id)
        and not exists (select 1 from layer l where l.project_id = p.id)
        and not exists (select 1 from gate_run g where g.project_id = p.id);
end $$;

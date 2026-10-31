-- Handoffs of maestro sessions were filed under the worktree's folder name (autonome-task-22-...)
-- instead of their project. Move every handoff that reports on a task into the task's project,
-- with its feature and layers, and drop the projects left empty.
do $$
declare
    h record;
    target_feature bigint;
begin
    for h in
        select d.id, t.project_id as target, f.slug as feature
        from document d
        join link l on l.from_id = d.id and l.kind = 'reports'
        join document t on t.id = l.to_id and t.type = 'task'
        left join feature f on f.id = d.feature_id
        where d.type = 'handoff' and d.project_id <> t.project_id
    loop
        target_feature := null;
        if h.feature is not null then
            insert into feature (project_id, slug, name) values (h.target, h.feature, h.feature)
                on conflict (project_id, slug) do update set slug = excluded.slug
                returning id into target_feature;
        end if;
        insert into layer (project_id, slug)
            select h.target, l.slug from document_layer dl join layer l on l.id = dl.layer_id
            where dl.document_id = h.id
            on conflict (project_id, slug) do nothing;
        update document_layer dl set layer_id = nl.id
            from layer ol, layer nl
            where dl.document_id = h.id and ol.id = dl.layer_id and nl.project_id = h.target and nl.slug = ol.slug;
        update document set project_id = h.target, feature_id = target_feature where id = h.id;
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

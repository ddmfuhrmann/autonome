-- Before the scribe marked deviations, every decision it recorded was "proposed" and counted as waiting
-- on the human. Under the current rule a recorded choice that is not a deviation is a decision in effect.
update document d set status = 'accepted'
where d.type = 'decision' and d.status = 'proposed'
  and exists (select 1 from link l join document h on h.id = l.from_id and h.type = 'handoff'
              where l.to_id = d.id and l.kind = 'records')
  and not coalesce((select (v.body->>'deviation')::boolean from document_version v
                    where v.document_id = d.id order by v.version desc limit 1), false);

-- The forward queue of a project, imported from its roadmap: one backlog document per roadmap wave,
-- with its items. compose turns a wave of it into an autonome wave of plans.
alter table document drop constraint document_type_check;
alter table document add constraint document_type_check check (type in
    ('plan', 'task', 'handoff', 'decision', 'convention', 'gate_config', 'design', 'feature_doc', 'wave', 'backlog'));

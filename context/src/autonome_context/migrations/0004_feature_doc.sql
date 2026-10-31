-- The living documentation: one feature_doc per feature (and one with no feature: the system
-- overview), rewritten by the scribe after each delivery. Versions keep its history.
alter table document drop constraint document_type_check;
alter table document add constraint document_type_check check (type in
    ('plan', 'task', 'handoff', 'decision', 'convention', 'gate_config', 'design', 'feature_doc'));

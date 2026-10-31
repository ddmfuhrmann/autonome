-- A wave groups the plans of several features, approved together and played in dependency order.
alter table document drop constraint document_type_check;
alter table document add constraint document_type_check check (type in
    ('plan', 'task', 'handoff', 'decision', 'convention', 'gate_config', 'design', 'feature_doc', 'wave'));

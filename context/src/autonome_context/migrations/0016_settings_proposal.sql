-- The settings the init proposes for a project. A proposal is not a gate config version: only the
-- human turns it into one, in the Settings, so it lives on the project and no config reader sees it.
alter table project add column settings_proposal jsonb;

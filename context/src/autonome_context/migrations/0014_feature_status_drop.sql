-- Reverts 0013 by the human's choice: a new feature enters the catalog through resolve_feature,
-- with no approval step, so the status column goes away.
alter table feature drop column status;

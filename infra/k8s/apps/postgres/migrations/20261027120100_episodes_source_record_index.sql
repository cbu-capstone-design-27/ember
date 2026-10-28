-- EMBER-85: extract finds the source_records it has not extracted yet with an anti-join on
-- episodes.source_record_id, and deleting a source record cascades to its episodes. Both look
-- episodes up by source_record_id.

-- migrate:up
create index episodes_source_record on episodes (source_record_id);

-- migrate:down
drop index episodes_source_record;

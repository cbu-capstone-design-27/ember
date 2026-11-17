-- EMBER-75: the enrich marker on raw_events and the index enrich workers claim unenriched rows by
-- (docs/ingestion/architecture.md, "Interim: claiming without the job queue"). The doc's Storage
-- SQL is the source of truth for these.

-- migrate:up
alter table raw_events add column enriched_at timestamptz;   -- set by that source's enrich worker in the same transaction as its source_records write

create index raw_events_unenriched on raw_events (source, id) where enriched_at is null;

-- migrate:down
drop index raw_events_unenriched;
alter table raw_events drop column enriched_at;

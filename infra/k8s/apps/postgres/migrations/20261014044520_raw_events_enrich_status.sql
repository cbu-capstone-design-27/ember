-- EMBER-67: the enrich stage's status on each raw event, as docs/ingestion/architecture.md (Storage)
-- defines it. Enrich workers claim rows where enriched_at is null ("How enrich wakes and claims rows").
-- The doc is the source of truth; test_migrations.py fails if these statements and the doc's build
-- different schemas.

-- migrate:up
alter table raw_events
  add column enriched_at     timestamptz,
  add column enrich_attempts int  not null default 0,
  add column enrich_error    text;

create index raw_events_enrich_pending on raw_events (source, id) where enriched_at is null;

-- migrate:down
drop index raw_events_enrich_pending;

alter table raw_events
  drop column enrich_error,
  drop column enrich_attempts,
  drop column enriched_at;

-- EMBER-63: the source ordering value on raw_events, the time the source says the event happened
-- (docs/ingestion/architecture.md, "Source ordering value"), and the index enrich claims by it. The
-- doc's Storage SQL is the source of truth for these. The default is the insert time, the same as
-- received_at, which is every source's fallback, so a writer that doesn't set it yet keeps working.

-- migrate:up
alter table raw_events add column occurred_at timestamptz not null default now();   -- the source's time; whoever writes the row sets it

create index raw_events_unenriched_occurred_at on raw_events (source, occurred_at, id) where enriched_at is null;

-- migrate:down
drop index raw_events_unenriched_occurred_at;
alter table raw_events drop column occurred_at;

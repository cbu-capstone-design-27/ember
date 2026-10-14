-- EMBER-75: what the per-source enrich workers need until the job queue lands
-- (docs/ingestion/architecture.md, "Interim: claiming without the job queue"). An enrich marker on
-- raw_events, an index to claim unenriched rows by, and a NOTIFY on channel raw_events, payload =
-- the source, for every insert. The doc's Storage SQL is the source of truth for these.

-- migrate:up
alter table raw_events add column enriched_at timestamptz;   -- set by that source's enrich worker in the same transaction as its source_records write

create index raw_events_unenriched on raw_events (source, id) where enriched_at is null;

create function raw_events_notify() returns trigger language plpgsql as $$
begin
  perform pg_notify('raw_events', new.source);
  return null;
end
$$;

create trigger raw_events_notify after insert on raw_events
  for each row execute function raw_events_notify();

-- migrate:down
drop trigger raw_events_notify on raw_events;
drop function raw_events_notify();
drop index raw_events_unenriched;
alter table raw_events drop column enriched_at;

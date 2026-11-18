-- EMBER-111: the metrics exporter counts ingestion by source and tenant for the Ember ingestion
-- dashboard (apps/postgres/monitoring.yaml): raw_events by origin, source_records by kind and how many
-- are embedded, rate limits, and which accounts are mapped to which tenant. It reads only these
-- columns, never an envelope, a record or an embedding. Like the earlier grants, a Postgres without
-- the role (CI, a laptop) skips them.

-- migrate:up
do $$
begin
  if exists (select from pg_roles where rolname = 'cnpg_metrics_exporter') then
    grant select (origin) on raw_events to cnpg_metrics_exporter;
    grant select (source, tenant, kind, created_at, embedded_at) on source_records to cnpg_metrics_exporter;
    grant select (source, tenant, bucket, next_allowed_at, remaining) on rate_limits to cnpg_metrics_exporter;
    grant select (source, account_id, tenant, added_at) on installations to cnpg_metrics_exporter;
  end if;
end
$$;

-- migrate:down
do $$
begin
  if exists (select from pg_roles where rolname = 'cnpg_metrics_exporter') then
    revoke select (origin) on raw_events from cnpg_metrics_exporter;
    revoke select (source, tenant, kind, created_at, embedded_at) on source_records from cnpg_metrics_exporter;
    revoke select (source, tenant, bucket, next_allowed_at, remaining) on rate_limits from cnpg_metrics_exporter;
    revoke select (source, account_id, tenant, added_at) on installations from cnpg_metrics_exporter;
  end if;
end
$$;

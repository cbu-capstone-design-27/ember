-- EMBER-91: CloudNativePG's metrics exporter counts the enrich backlog and the unmapped rows
-- (apps/postgres/monitoring.yaml). It runs as role cnpg_metrics_exporter, which can read only what it
-- is granted: these columns, never the envelope. The operator creates the role on the cluster; a
-- Postgres without it (CI, a laptop) skips the grant.

-- migrate:up
do $$
begin
  if exists (select from pg_roles where rolname = 'cnpg_metrics_exporter') then
    grant select (source, account_id, tenant, received_at, enriched_at) on raw_events to cnpg_metrics_exporter;
  end if;
end
$$;

-- migrate:down
do $$
begin
  if exists (select from pg_roles where rolname = 'cnpg_metrics_exporter') then
    revoke select (source, account_id, tenant, received_at, enriched_at) on raw_events from cnpg_metrics_exporter;
  end if;
end
$$;

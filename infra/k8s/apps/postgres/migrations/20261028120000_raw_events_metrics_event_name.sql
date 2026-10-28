-- The metrics exporter also counts stored deliveries by source and event type, for the dashboard's
-- per-source event panels (apps/postgres/monitoring.yaml, raw_events_received). Slack, unlike GitHub,
-- sends the event type only in the body, so nginx's access log can't show it. event_name is the
-- adapter's event type, never payload content. Like 20261015120000_raw_events_metrics_grant.sql, a
-- Postgres without the role (CI, a laptop) skips the grant.

-- migrate:up
do $$
begin
  if exists (select from pg_roles where rolname = 'cnpg_metrics_exporter') then
    grant select (event_name) on raw_events to cnpg_metrics_exporter;
  end if;
end
$$;

-- migrate:down
do $$
begin
  if exists (select from pg_roles where rolname = 'cnpg_metrics_exporter') then
    revoke select (event_name) on raw_events from cnpg_metrics_exporter;
  end if;
end
$$;

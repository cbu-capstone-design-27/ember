-- EMBER-95: map one source account to a tenant, then hand the deliveries it already stored to the
-- enrich workers. Interim, until `ember-admin map` (docs/ingestion/architecture.md). Run by
-- playbooks/tenants.yml as `psql -v source=... -v account=... -v tenant=...`; :'name' quotes each value.
\set ON_ERROR_STOP on

insert into installations (source, account_id, tenant)
values (:'source', :'account', :'tenant')
on conflict (source, account_id) do nothing;

select tenant as current_tenant from installations
where source = :'source' and account_id = :'account' \gset

select :'current_tenant' <> :'tenant' as remap \gset

\if :remap
  -- Moving an account between tenants would split its records across two subgraphs
  \echo refused: :source account :account is already mapped to :current_tenant
\else
  -- After the mapping commits, so intake stores new deliveries with the tenant and this catches the
  -- rest. A delivery intake was storing at that very moment can still land without one; run again
  with mapped as (
    update raw_events set tenant = :'tenant'
    where source = :'source' and account_id = :'account' and tenant is null
    returning 1
  )
  select count(*) as mapped from mapped \gset

  -- The insert trigger doesn't fire on an update. Wake the workers instead of waiting for their poll
  select pg_notify('raw_events', :'source') \gset

  \echo mapped: :source account :account to :tenant, :mapped stored deliveries handed to enrich
\endif

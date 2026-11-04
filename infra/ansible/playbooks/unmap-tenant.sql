-- Remove one source account's mapping to a tenant, so another tenant can connect it. Deletes only the
-- installations row: the deliveries, records and graph the account already produced stay with the
-- tenant they were stored under. Deliveries that arrive afterwards are stored with tenant null until
-- the account is mapped or connected again, which hands them on. Run by playbooks/tenants.yml as
-- `psql -v source=... -v account=... -v tenant=...`; :'name' quotes each value.
\set ON_ERROR_STOP on

begin;

select count(*) > 0 as held from installations
where source = :'source' and account_id = :'account' and tenant = :'tenant' \gset

-- A connected account has credentials, and its source still holds hooks pointing at us (Jira's
-- dynamic webhooks, for one). Disconnecting it in Settings removes those first; deleting the row
-- here would cascade the credentials away and leave the hooks behind.
select count(*) as credentials from source_credentials
where source = :'source' and account_id = :'account' \gset

\if :held
  select :credentials > 0 as connected \gset
  \if :connected
    \echo refused: :source account :account has :credentials stored credentials; disconnect it in Settings instead
  \else
    delete from installations where source = :'source' and account_id = :'account' and tenant = :'tenant';
    \echo unmapped: :source account :account from :tenant
  \endif
\else
  select coalesce(max(tenant), '') as current_tenant from installations
  where source = :'source' and account_id = :'account' \gset
  select :'current_tenant' <> '' as mapped_elsewhere \gset
  \if :mapped_elsewhere
    \echo refused: :source account :account is mapped to :current_tenant, not :tenant
  \else
    -- Running it again changes nothing
    \echo unmapped: :source account :account was not mapped
  \endif
\endif

commit;

-- EMBER-130: what the web app records when an owner connects a source (ADR 0010, "The connection
-- record"). The doc's Storage SQL is the source of truth for these. Every column is nullable, so the
-- rows `make map-tenant` wrote keep working: they are connected (disconnected_at is null), and have no
-- label, external id or connecting user.

-- migrate:up
alter table installations
  add column disconnected_at timestamptz,   -- null while connected; intake maps only connected accounts
  add column label text,                    -- what Settings shows: GitHub login, Slack workspace, Jira site, GitLab group
  add column external_id text,              -- the source's stable id for the account, where account_id is not (GitHub: installation.account.id)
  add column connected_by text;             -- the web app's user id; null for make map-tenant

-- A reinstall gets a new installation id, so a move to another tenant is caught by the account's id
create index installations_external_id on installations (source, external_id) where external_id is not null;

-- migrate:down
drop index installations_external_id;
alter table installations
  drop column connected_by,
  drop column external_id,
  drop column label,
  drop column disconnected_at;

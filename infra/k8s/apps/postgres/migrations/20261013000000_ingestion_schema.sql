-- EMBER-61: the ingestion store as docs/ingestion/architecture.md (Storage) defines it. The doc is
-- the source of truth; test_migrations.py fails if these statements and the doc's build different
-- schemas. The doc's daily retention delete is not schema and is left out.

-- migrate:up
create table raw_events (
  id           bigint generated always as identity primary key,
  source       text        not null,              -- = envelope.type
  delivery_id  text        not null,              -- adapter delivery_id, or "<object id>@<version>" for reconcile
  account_id   text,
  tenant       text,                              -- null until the account is mapped
  event_name   text        not null,
  origin       text        not null check (origin in ('webhook', 'reconcile', 'backfill')),
  received_at  timestamptz not null default now(),
  completed_at timestamptz,                       -- set when everything it produced is loaded
  envelope     jsonb       not null,
  unique (source, delivery_id)
);

create table source_records (
  id           bigint generated always as identity primary key,
  source       text not null,
  tenant       text not null,
  kind         text not null,          -- "pull_request", "issue", "message", …
  object_id    text not null,          -- the source's own id: "org/repo#42", "C0123/1696.12"
  version      text not null,          -- updated_at, etag or SHA
  occurred_at  timestamptz not null,   -- the source's time
  record       jsonb not null,         -- payload object + fetched extras
  raw_event_id bigint references raw_events (id) on delete set null,   -- outlives its raw event
  created_at   timestamptz not null default now(),
  unique (source, tenant, object_id, version)
);

create table episodes (
  id                bigint generated always as identity primary key,
  key               text not null unique,     -- "github:pull_request:org/repo#42@<version>"
  tenant            text not null,
  source            text not null,
  kind              text not null,
  occurred_at       timestamptz not null,     -- Graphiti reference_time
  text              text not null,
  attrs             jsonb not null default '{}',
  source_record_id  bigint references source_records (id) on delete cascade,
  extractor_version int not null,
  loaded_at         timestamptz,              -- null = waiting for load
  updated_at        timestamptz not null default now()
);
create index episodes_unloaded on episodes (tenant, source, occurred_at) where loaded_at is null;

create table cursors (
  source text not null, tenant text not null,
  stream text not null,            -- "issues", "pulls", "channel:C0123", "project:42"
  position text not null,          -- timestamp, page token or SHA, as the source pages
  updated_at timestamptz not null default now(),
  primary key (source, tenant, stream)
);

create table installations (
  source text not null, account_id text not null,
  tenant text not null,            -- ontology subgraph_id rules: [a-z0-9][a-z0-9-]*
  added_at timestamptz not null default now(),
  primary key (source, account_id)
);

create table rate_limits (
  source text, tenant text, bucket text,   -- GitHub: installation · Slack: method × workspace · Jira: site
  next_allowed_at timestamptz not null,    -- Graph: app × tenant · GitLab: token
  remaining int,
  primary key (source, tenant, bucket)
);

-- migrate:down
drop table rate_limits, installations, cursors, episodes, source_records, raw_events;

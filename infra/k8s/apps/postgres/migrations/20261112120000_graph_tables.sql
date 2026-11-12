-- EMBER-164: the graph in Postgres (ADR 0011, Phase A), one graph per connected scope (a repo, a Jira
-- project, a Slack workspace), each owned by a tenant. load writes it; readers (MCP, web) read it
-- through role ember_graph_reader and row-level security on `ember.tenants`. The doc's Storage SQL is
-- the source of truth, and this DDL is pinned with the Graphiti fork (EMBER-158).
--
-- Role ember_graph_reader: on the cluster the operator creates it (cluster.yaml, managed roles),
-- because role ember, which runs the migrations, cannot create roles. Elsewhere (CI, a laptop) this
-- creates it. Roles are cluster-wide, so the down migration revokes its grants but keeps the role.
-- uuid-ossp is a trusted extension, so role ember may create it. It gives uuid_generate_v5 for the
-- episodes.uuid backfill, in loader/records.py's namespace.

-- migrate:up
create extension if not exists "uuid-ossp";

do $$
begin
  if not exists (select from pg_roles where rolname = 'ember_graph_reader') then
    create role ember_graph_reader nologin;
  end if;
end
$$;

create schema graph;

create table graph.graphs (
  id         bigint generated always as identity primary key,
  tenant     text not null,
  source     text not null,          -- github | gitlab | jira | slack | teams | code
  scope      text not null,          -- ontology.graph_scope: owner/repo, host/PROJECT, the Slack team id
  name       text,
  group_id   text not null unique,   -- Graphiti group_id: {tenant}_{source}_{id}
  created_at timestamptz not null default now(),
  unique (tenant, source, scope)
);

create table graph.nodes (
  id          bigint generated always as identity primary key,
  uuid        uuid not null unique,  -- uuid5 of (graph group_id, label, key)
  graph_id    bigint not null references graph.graphs on delete cascade,
  tenant      text not null,         -- copied from the graph, for row-level security and filters
  label       text not null,
  key         text not null,
  name        text,
  summary     text,
  props       jsonb not null default '{}',
  placeholder boolean not null default false,
  deleted     boolean not null default false,
  occurred_at timestamptz,
  embed_text  text,
  embedding   vector(1024),
  created_at  timestamptz not null default now(),
  search      tsvector generated always as (to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(embed_text, ''))) stored,
  unique (graph_id, label, key)
);
create index nodes_tenant on graph.nodes (tenant);
create index nodes_embedding on graph.nodes using hnsw (embedding vector_cosine_ops);
create index nodes_search on graph.nodes using gin (search);

create table graph.edges (
  id             bigint generated always as identity primary key,
  uuid           uuid not null unique,
  graph_id       bigint references graph.graphs on delete cascade,   -- null for link edges between graphs
  tenant         text not null,
  type           text not null,
  fact           text,
  fact_embedding vector(1024),
  src            bigint not null references graph.nodes on delete cascade,
  dst            bigint not null references graph.nodes on delete cascade,
  props          jsonb not null default '{}',
  valid_at       timestamptz,          -- null = unknown (Graphiti); Ember's writer always sets it
  invalid_at     timestamptz,
  created_at     timestamptz not null default now(),
  expired_at     timestamptz,
  episode_id     bigint references episodes (id) on delete set null,
  invalidated_by bigint references episodes (id) on delete set null,
  search         tsvector generated always as (to_tsvector('simple', coalesce(fact, ''))) stored
);
create index edges_fact_embedding on graph.edges using hnsw (fact_embedding vector_cosine_ops);
create index edges_search on graph.edges using gin (search);
create unique index edges_current on graph.edges (src, type, dst, md5(coalesce(fact, ''))) where invalid_at is null and expired_at is null;
create index edges_out on graph.edges (src, type) include (dst, valid_at, invalid_at) where expired_at is null;
create index edges_in  on graph.edges (dst, type) include (src, valid_at, invalid_at) where expired_at is null;

create table graph.mentions (
  node_id    bigint references graph.nodes on delete cascade,
  episode_id bigint references episodes (id) on delete cascade,
  primary key (node_id, episode_id)
);

alter table episodes add column uuid uuid unique;   -- Graphiti EpisodicNode uuid; uuid5 of episodes.key
alter table episodes
  add column graph_id bigint references graph.graphs on delete cascade,   -- set by load when it resolves the graph
  add column name text,                    -- Ember's episodes: = key
  add column source_description text,
  add column created_at timestamptz not null default now();
create index episodes_graph on episodes (graph_id, occurred_at);
update episodes set uuid = uuid_generate_v5('6f1c2b4e-0a1d-4c53-9d57-3f4f0f8c2a10', key), name = key;

-- Row-level security. Readers set `ember.tenants` to a comma-separated list per transaction; unset
-- sees nothing. The table owner (load) bypasses it.
alter table graph.graphs   enable row level security;
alter table graph.nodes    enable row level security;
alter table graph.edges    enable row level security;
alter table graph.mentions enable row level security;
create policy tenant_read on graph.graphs to ember_graph_reader
  using (tenant = any(string_to_array(current_setting('ember.tenants', true), ',')));
create policy tenant_read on graph.nodes to ember_graph_reader
  using (tenant = any(string_to_array(current_setting('ember.tenants', true), ',')));
create policy tenant_read on graph.edges to ember_graph_reader
  using (tenant = any(string_to_array(current_setting('ember.tenants', true), ',')));
create policy tenant_read on graph.mentions to ember_graph_reader
  using (exists (select from graph.nodes n where n.id = node_id));

-- source_records and episodes: the same tenant policy for the reader, and every other role (the
-- workers, the metrics exporter) keeps seeing every row.
alter table source_records enable row level security;
alter table episodes       enable row level security;
create policy tenant_read on source_records to ember_graph_reader
  using (tenant = any(string_to_array(current_setting('ember.tenants', true), ',')));
create policy tenant_read on episodes to ember_graph_reader
  using (tenant = any(string_to_array(current_setting('ember.tenants', true), ',')));
create policy not_reader on source_records
  using (not pg_has_role(current_user, 'ember_graph_reader', 'member'));
create policy not_reader on episodes
  using (not pg_has_role(current_user, 'ember_graph_reader', 'member'));

grant usage on schema graph to ember_graph_reader;
grant select on graph.graphs, graph.nodes, graph.edges, graph.mentions, episodes, source_records to ember_graph_reader;

-- migrate:down
revoke select on episodes, source_records from ember_graph_reader;
drop policy not_reader on episodes;
drop policy not_reader on source_records;
drop policy tenant_read on episodes;
drop policy tenant_read on source_records;
alter table episodes       disable row level security;
alter table source_records disable row level security;
drop index episodes_graph;
alter table episodes drop column created_at, drop column source_description, drop column name,
  drop column graph_id, drop column uuid;
drop schema graph cascade;
drop extension "uuid-ossp";

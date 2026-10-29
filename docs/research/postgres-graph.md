# Postgres as Ember's graph store

Jira: EMBER-117 (Postgres graph store), EMBER-118 (a Graphiti fork on Postgres). Date: 2026-10-28, fork design 2026-10-29. Status: research, for a team decision. If accepted it supersedes [ADR 0002](../adr/0002-graph-engine-and-schema-approach.md)'s choice of Neo4j and Graphiti, not its ontology.

Diagrams (dark theme; open at [excalidraw.com](https://excalidraw.com) with Open): [`ember-ingestion-to-graph`](diagrams/ember-ingestion-to-graph.excalidraw), [`ember-postgres-subgraphs`](diagrams/ember-postgres-subgraphs.excalidraw), [`ember-graphiti-postgres`](diagrams/ember-graphiti-postgres.excalidraw), [`ember-graph-options`](diagrams/ember-graph-options.excalidraw). Each diagram carries its own legend and numbered notes. Benchmark script: [`postgres-graph-bench.py`](postgres-graph-bench.py).

## Recommendation

Store the graph in the Postgres we already run, as two plain tables (`graph.nodes`, `graph.edges`) with time columns on edges, queried with joins and recursive CTEs. Do not use Apache AGE, and do not wait for SQL/PGQ: AGE needs a custom CloudNativePG image and was the slowest engine here, and SQL/PGQ was pulled from PostgreSQL 19 before release. Plain tables answered every benchmark query in under 3 ms at 100 tenants, fastest or tied on all four, put vector search and graph expansion in one SQL statement, and remove Neo4j as a second database to run, back up and keep in step.

Most of the pipeline already exists. `extract` (EMBER-85) maps each `source_records` row to an `episodes` row holding the ontology's nodes and edges. Only the `load` stage changes: it writes those episodes into the graph tables instead of Neo4j, in the same transaction that marks them loaded.

For the Graphiti side (EMBER-118), fork Graphiti (Apache-2.0) and add a Postgres driver on these same tables. Graphiti core keeps doing episodes, LLM extraction, entity resolution, bi-temporal edges, search and rerank. Ember adds its ontology, `{tenant}_{source}` group ids, and a bulk path that writes the deterministic mappers' output without LLM calls. The driver is about 100 interface methods in SQL plus seven small hooks in core where Graphiti still runs Cypher directly. Details in [Part 2](#part-2-ember-118-a-graphiti-fork-on-postgres).

Effort: about 2 weeks for the Postgres store, 3 to 4 weeks for the fork and its driver, 1 week for the MCP tools, 2 to 3 weeks for the code graph. Details in [Effort](#effort).

## Where the pipeline is today

On the cluster, data stops at the embed worker: `raw_events` → enrich (GitHub, Jira, Slack) → `source_records` → embed. `infra/k8s/apps` deploys intake, the three enrich workers, embed and Neo4j, and no extract or load.

In the repo, `services/pipeline/loader` already has both later stages, on ADR 0002's fallback path with no LLM ([`services/pipeline/README.md`](../../services/pipeline/README.md#extract-and-load-workers-ember-85)):

- `extract` wakes on `NOTIFY source_records`, maps each row with EMBER-45's mappers, validates against the ontology, and writes one `episodes` row whose `attrs` holds the nodes and edges.
- `load` wakes on `NOTIFY episodes`, takes one subgraph under `pg_try_advisory_xact_lock(hashtext('{tenant}_{source}'))`, applies up to 100 episodes in `occurred_at` order to Neo4j, and stamps `loaded_at`.

It has run on photon and on a copy of the Parallax GitHub data ([`neo4j-load-check.md`](../pipeline/neo4j-load-check.md)). So "map `source_records` to the 9 node and 16 edge types" is written and tested. What is missing is a graph store that fits the rest of the system, time on edges, cross-source links, and retrieval over the graph.

## Options

Checked on 2026-10-28.

| | Plain tables + recursive CTEs + pgvector | Apache AGE | SQL/PGQ | Neo4j (today) |
|---|---|---|---|---|
| What exists | Postgres 18.6 on the cluster (`ghcr.io/cloudnative-pg/postgresql:18.6-system-trixie`), pgvector already used | AGE 1.7.0 for PG18 released 2026-01-21; 1.8.0 tagged `PG18/v1.8.0-rc0` on 2026-07-08 | In **no released Postgres**. Committed for 19, shipped in 19 Beta 1 (2026-06-04), reverted in Beta 4 (2026-09-24). 19 GA is planned for 2026-11-19 without it. Earliest is 20, about a year out | Helm chart 2026.9.0, Community, 1 CPU and 2 Gi on `hv-worker-1` |
| Ontology and subgraphs | `label`/`type` columns checked against `ENTITY_TYPES`/`EDGE_TYPES`; subgraph = `(tenant, source)` columns | One table per label; subgraph as a property or one AGE graph (schema) per subgraph | A view over tables, so it would sit on the plain-tables design | Labels; `group_id` property |
| Temporal queries | `valid_at`/`invalid_at` columns, partial indexes on current edges | Properties inside `agtype`, no index support unless added per expression | Same as plain tables | Properties; indexed only if we add range indexes |
| Vector + graph in one query | Yes: one SQL statement, HNSW index | No: `agtype` cannot hold a pgvector `vector`, so vector search runs in SQL and the walk in a second `cypher()` call | Would be, via the plain tables | Vector index exists, but 5.26 has no tenant filter, so it over-fetches (48 ms below) |
| Operations | Nothing new. One CNPG backup covers raw data, records, embeddings and the graph | Not in CNPG's images or its [extension images](https://github.com/cloudnative-pg/postgres-extensions-containers) (pgAudit, pg_crash, pg_ivm, pgRouting, pgvector, PostGIS, TimescaleDB, wal2json). We would build and patch our own image. 1.8 wants `shared_preload_libraries` | Nothing new, once it exists | A second database: its own backup, upgrades, memory on a 4 GB node, and a write that cannot commit with `episodes.loaded_at` |
| Maintenance | Postgres core | Maintainers describe "a small team of developers" with limited resources ([discussion #2305](https://github.com/apache/age/discussions/2305), 2026-01-16) | Postgres core, later | Neo4j, Inc. |
| Benchmark (100 tenants) | 0.8 to 2.8 ms, fastest or tied on all four | 524 ms for an untyped 1-hop query | Not testable | 5 to 48 ms |
| Verdict | **Use** | No | Watch for 20; the tables can get a `CREATE PROPERTY GRAPH` over them then | Drop |

Graphiti as shipped has no Postgres backend, which is why Part 2 forks it: graphiti-core 0.31.2 (2026-10-20) supports Neo4j 5.26, FalkorDB, Amazon Neptune and Kuzu (deprecated). Requests for a Postgres backend ([#779](https://github.com/getzep/graphiti/issues/779), pgvector; [#1095](https://github.com/getzep/graphiti/issues/1095), AGE) were closed without one.

### Benchmark

Photon as `extract` maps it (1,140 nodes, 2,327 edges, 666 messages with vectors), copied once per tenant. The Parallax GitHub load had 2,391 nodes and about 11,000 edges ([`neo4j-load-check.md`](../pipeline/neo4j-load-check.md#github-the-parallax-repository-ember-107)), so one tenant is photon-to-Parallax size and 100 tenants is 114,000 nodes, 232,700 edges and 66,600 random 1024-dim vectors. Postgres 18.6 with pgvector 0.8.7 and AGE 1.8.0-rc0 in one scratch container, Neo4j 5.26.32 Community (the compose image) in another, on an Apple M3 Pro under Docker Desktop. Each query runs 30 times after one warm-up, from Python on the same host, with a random tenant and start node. All engines returned the same answers. Median / p95 ms:

| Query | Postgres tables, 1 / 100 tenants | AGE 1.8, 1 / 100 | Neo4j 5.26, 1 / 100 |
|---|---|---|---|
| 1-hop neighbours of a ticket | 1.0 / 0.8 | 24.2 / **524.3** | 4.4 / 4.5 |
| Co-participants of a person (Person→Message→Conversation←Message←Person) | 1.3 / 2.8 | 0.5 / 2.8 | 4.1 / 6.8 |
| Everything within 3 hops of a ticket | 1.5 / 1.5 | 5.1 / 4.7 | 5.2 / 4.9 |
| Vector top-10 messages, with thread and author | 2.5 / 2.6 | n/a | 5.8 / **47.7** |

- Every query is a few milliseconds on plain tables at both sizes, because a walk only touches its neighbourhood and every edge lookup is an index range scan. Size does not matter at Ember's scale. Hubs do: a 3-hop walk from a ticket reaches 146 nodes through its project's `CONTAINS` edges.
- AGE's untyped 1-hop pattern (`-[]-`) took 524 ms, while its untyped variable-length 3-hop pattern took 4.7 ms. Measured, not yet explained.
- Neo4j's vector query has to fetch 1,100 candidates to find 10 in one tenant. Postgres filters inside the HNSW scan (`hnsw.iterative_scan`, as `apps/mcp` already does).
- Load times at 100 tenants: Postgres 55 s (mostly the HNSW build), AGE 3 s through its CSV loader (no vectors), Neo4j 102 s including the vector index. None of this matters for incremental load.

Not measured: temporal filters with real history (photon has none), and concurrent writes.

## Part 1 (EMBER-117): from `source_records` to subgraphs in Postgres

### Flow

```
source_records ──NOTIFY source_records──▶ extract ──▶ episodes ──NOTIFY episodes──▶ load ──▶ graph.nodes / graph.edges / graph.mentions
       └──────NOTIFY source_records──▶ embed (unchanged)                                └──▶ link (same transaction)
```

Only load's writer changes, and a link step is added. Extract, the episode format and the claim logic stay.

### Tables

```sql
create schema graph;

-- One row per node. A subgraph is (tenant, source); source 'code' holds the code graph.
create table graph.nodes (
  id          bigint generated always as identity primary key,
  uuid        uuid not null unique,   -- Graphiti's id: uuid5 of (tenant, source, label, key)
  tenant      text not null,
  source      text not null,          -- github | gitlab | jira | slack | teams | code
  label       text not null,          -- ontology ENTITY_TYPES (plus File and Symbol for code)
  key         text not null,          -- the mapper's key; external_id for source-backed nodes
  name        text,
  summary     text,                   -- Graphiti's entity summary
  props       jsonb not null default '{}',   -- Graphiti's attributes
  placeholder boolean not null default false,   -- created by an edge before its node's own episode
  deleted     boolean not null default false,
  occurred_at timestamptz,            -- source time of the newest version applied
  embed_text  text,
  embedding   vector(1024),           -- ADR 0001; Graphiti's name_embedding
  search      tsvector generated always as (to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(embed_text, ''))) stored,
  unique (tenant, source, label, key)
);
create index nodes_embedding on graph.nodes using hnsw (embedding vector_cosine_ops);
create index nodes_search on graph.nodes using gin (search);

-- One row per fact. valid_at/invalid_at: when it was true in the source. created_at/expired_at: when Ember held it.
create table graph.edges (
  id             bigint generated always as identity primary key,
  uuid           uuid not null unique,
  tenant         text not null,
  source         text not null,       -- the subgraph that asserted it; 'link' for cross-subgraph edges
  type           text not null,       -- ontology EDGE_TYPES; Graphiti's edge name
  fact           text,                -- one sentence, "Ryan is assigned to PHO-15"; what edge search matches
  fact_embedding vector(1024),
  src            bigint not null references graph.nodes on delete cascade,
  dst            bigint not null references graph.nodes on delete cascade,
  props          jsonb not null default '{}',   -- verdict, link_type, confidence, method
  valid_at       timestamptz not null,
  invalid_at     timestamptz,
  created_at     timestamptz not null default now(),
  expired_at     timestamptz,
  episode_id     bigint references episodes (id) on delete set null,   -- what asserted it
  invalidated_by bigint references episodes (id) on delete set null,   -- what ended it: the "why"
  search         tsvector generated always as (to_tsvector('simple', coalesce(fact, ''))) stored
);
create index edges_fact_embedding on graph.edges using hnsw (fact_embedding vector_cosine_ops);
create index edges_search on graph.edges using gin (search);
create unique index edges_current on graph.edges (src, type, dst) where invalid_at is null and expired_at is null;
create index edges_out on graph.edges (src, type) include (dst, valid_at, invalid_at) where expired_at is null;
create index edges_in  on graph.edges (dst, type) include (src, valid_at, invalid_at) where expired_at is null;

-- Which episodes mention which nodes: provenance, node history, and a rerank signal.
create table graph.mentions (
  node_id    bigint references graph.nodes on delete cascade,
  episode_id bigint references episodes (id) on delete cascade,
  primary key (node_id, episode_id)
);
```

`nodes.label` and `edges.type` stay free text, checked in code against the ontology the way `neo4j_writer.py` whitelists labels today, so a new type is a code change and not a migration. `EmberConfig` becomes one row in a `graph.config` table. The `uuid`, `summary`, `fact` and `fact_embedding` columns are what Graphiti's models need (Part 2). `episodes` gains a `uuid` column for the same reason.

### Trigger, claim and batching

- **Trigger.** Keep the chain that exists: extract listens on `source_records`, load on `episodes`, both with a 30 s poll and a catch-up at start. Do not trigger load from `source_records`: load needs extract's output, and the episode row is the unit of replay. When the job queue lands (ADR 0004), the same two jobs move onto it.
- **Claim.** Load already claims per subgraph with `pg_try_advisory_xact_lock(hashtext('{tenant}_{source}'))` and takes up to `LOAD_BATCH` (100) unloaded episodes in `(occurred_at, id)` order. Keep it. Extract claims with `for update skip locked` like the enrich workers.
- **One transaction.** With Neo4j, the graph write and `episodes.loaded_at` are two commits in two systems. With Postgres they are one: nodes, edges, mentions, link edges and `loaded_at` commit together or not at all.
- **Writes per batch.** Group the batch's nodes by label and upsert with `insert ... on conflict (tenant, source, label, key) do update`, then edges with one `insert ... select` from `unnest` arrays. Rows for the same node apply in order inside the batch, which is what `_rounds` does for Cypher today.

### Idempotency, versions and deletes

These are the rules `neo4j_writer.py` already enforces, moved to SQL.

- **Versions.** An episode is unique per `source_records` row (`key`), and that row per `(source, tenant, object_id, version)`. Re-running load on a loaded episode changes nothing.
- **Newest wins.** A node takes an episode's props only if `episode.occurred_at >= node.occurred_at`. An older episode only fills props the node lacks (`props = excluded.props || nodes.props`), and never touches a deleted node.
- **Deletes.** A tombstone episode not older than the node sets `deleted = true`, clears `props`, `name`, `embed_text` and `embedding`, and sets `invalid_at` on every current edge of that node, with `invalidated_by` = the tombstone's episode. The node row stays, so a late older edit cannot bring it back.
- **Placeholders.** An edge to a node not loaded yet creates the node with `placeholder = true` on its key. The node's own episode fills it in. Reads skip placeholders.
- **Edges that disappear** ([gap 10](../pipeline/neo4j-load-check.md#gaps-in-the-ontology-and-mapping-with-suggested-tickets)). Each mapper declares which edge types around its record's main node the record lists in full, for example a PR's `TOUCHES` and `RESOLVES`, or an issue's `ASSIGNED_TO`. When a newer version of that object loads, current edges of those types missing from the new episode get `invalid_at = episode.occurred_at` and `invalidated_by = episode.id`. An older version never invalidates.
- **Re-extraction.** Bumping `EXTRACTOR_VERSION` re-extracts and clears `loaded_at`. Load then sets `expired_at = now()` on the edges whose `episode_id` it replaces and inserts the new ones, so a corrected mapping is visible as a correction and not as a change in the source.

### Time: what was true when, and why it changed

| Question | Query |
|---|---|
| Who is assigned to PHO-15 now? | `type = 'ASSIGNED_TO' and dst = :wi and invalid_at is null and expired_at is null` |
| Who was assigned on 2026-10-01? | `valid_at <= :t and (invalid_at is null or invalid_at > :t) and expired_at is null` |
| Why did it change? | `invalidated_by` and the new edge's `episode_id` point at episodes, which point at the `source_records` row and its full payload |
| What was the ticket's status on 2026-10-01? | The newest `graph.mentions` episode for the node with `occurred_at <= :t`; its `attrs` holds the node's props as of that version |

Node property history is not copied into the graph: every version is already an episode. `Decision` nodes keep `SUPERSEDES`, as `docs/graph-schema.md` says.

### Entity resolution across sources

Each subgraph keeps its own `Identity` and `Person` nodes, as `docs/graph-schema.md` requires. The link step runs inside load's transaction, after the batch, for the nodes the batch touched:

- **People.** A `Person` whose email is set or changed gets a `SAME_AS` edge (source `link`, `props.method = 'email'`) to every other `Person` in the tenant with the same lowercased email. Weaker matches (same display name, a GitHub commit email against a Slack profile) are written with `props.method` and `props.confidence` and `valid_at`, but retrieval only follows `method = 'email'` until someone confirms them. A wrong merge is one edge to invalidate. `SAME_AS` is a new edge type (open question 1).
- **Keys in text.** A Jira key (`PHO-15`) or a `#n` in a Slack message, PR title or commit message becomes `REFERENCES` from the message to the `WorkItem` in the Jira or GitHub subgraph, source `link`. Keys are matched only against the tenant's known Jira project keys, so `UTF-8` is not a ticket. If the issue has not loaded yet, the edge creates a placeholder in the Jira subgraph, which the issue's own episode fills.
- **Paths and symbols.** See [code graph](#code-graph).

Retrieval in phase 1 can ignore `source = 'link'` edges and search one subgraph at a time; phase 2 follows them.

### Backfill and replay

- **Backfill** is reconcile: objects become `raw_events`, then the same path. Load's ordering rule makes an old object arriving after newer webhooks harmless. Its history is incomplete until a replay, because an older version arriving late never invalidates.
- **Replay load** for a subgraph: `delete from graph.nodes where tenant = :t and source = :s` (edges and mentions cascade), clear `loaded_at` on its episodes, and load rebuilds in `occurred_at` order with full history. This is `ember-admin replay --stage load` from ADR 0004, with no Neo4j involved.
- **Replay extract** after a mapper fix: bump `EXTRACTOR_VERSION`, as today.
- **Retention.** Replays read `episodes`. Their retention is still open in `architecture.md`, and this design depends on keeping them.

### Many tenants

- One table pair for every tenant. `(tenant, source)` leads the unique key, and walks follow `src`/`dst` ids, which never cross tenants except through `link` edges inside one tenant.
- The MCP and web roles read through row-level security on `tenant` (`current_setting('ember.tenant')`), which AGE and Neo4j Community cannot give us.
- Vector search filters tenant inside the HNSW scan (`hnsw.iterative_scan`). If one table holds hundreds of tenants and recall drops, list-partition `graph.nodes` by tenant so each tenant gets its own HNSW index. Not needed at 100 tenants.
- Purge (EMBER-53) is `delete from graph.nodes where tenant = :t` in the same transaction as the other tables.

### How retrieval reads it

`search` keeps its `source_records` vector search and adds graph hits (EMBER-118, below). `get_record` adds the record's node and its current 1-hop neighbours, so an agent sees the ticket, its PRs, its Slack threads and the people involved in one call.

## Part 2 (EMBER-118): a Graphiti fork on Postgres

Fork Graphiti instead of writing our own. Graphiti core already does the parts that are hard to get right (LLM extraction against an ontology, entity resolution, bi-temporal invalidation, hybrid search and rerank), and it is Apache-2.0. What it lacks is a Postgres backend. Everything below is from Graphiti v0.31.2 (tag `v0.31.2`, commit `d72407d`, 2026-10-20).

### How Graphiti abstracts storage today

| Layer | Files | What it is |
|---|---|---|
| Driver base | [`graphiti_core/driver/driver.py`](https://github.com/getzep/graphiti/blob/v0.31.2/graphiti_core/driver/driver.py) | `GraphDriver` ABC: `execute_query(cypher, **params)`, `session()`, `transaction()`, `build_indices_and_constraints()`. `GraphProvider` is a closed enum: `NEO4J`, `FALKORDB`, `KUZU`, `NEPTUNE` |
| Drivers | `driver/neo4j_driver.py`, `falkordb_driver.py`, `kuzu_driver.py`, `neptune_driver.py` | Each wraps a client and speaks Cypher (or its dialect) |
| Legacy hooks | [`driver/graph_operations/graph_operations.py`](https://github.com/getzep/graphiti/blob/v0.31.2/graphiti_core/driver/graph_operations/graph_operations.py) (85 methods), [`driver/search_interface/search_interface.py`](https://github.com/getzep/graphiti/blob/v0.31.2/graphiti_core/driver/search_interface/search_interface.py) (12) | Optional objects on the driver (`graph_operations_interface`, `search_interface`). Core calls them first, catches `NotImplementedError`, then falls back to Cypher. No driver in the repo sets them; the comment calls them "kept for backwards compatibility during Phase 1" |
| New operations | `driver/operations/*.py` (11 ABCs: entity, episode, community and saga nodes; entity, episodic, community, has-episode and next-episode edges; search; graph maintenance), one implementation per driver in `driver/<provider>/operations/` | Used only by the `graphiti.nodes` and `graphiti.edges` namespaces (`namespaces/`). `add_episode` and search do not use them yet |

**Where Cypher is still hard-coded in core**, with `match driver.provider` branches:

| File | Cypher statements | Hooked? |
|---|---|---|
| `search/search_utils.py` | 82 | Mostly through `search_interface`. Not hooked: `get_relevant_nodes`, `get_relevant_edges`, `get_edge_invalidation_candidates` |
| `models/edges/edge_db_queries.py`, `models/nodes/node_db_queries.py` | 58, 28 | Query builders per provider, used by the fallbacks |
| `nodes.py`, `edges.py` | 37, 33 | Every method checks `graph_operations_interface` first |
| `utils/bulk_utils.py` (`add_nodes_and_edges_bulk`) | per provider | Hooked (`node_save_bulk`, `edge_save_bulk`, `episodic_*_save_bulk`) |
| `graphiti.py` | 5 | Not hooked: `_get_or_create_saga`, `summarize_saga`, `remove_episode` |
| `utils/maintenance/edge_operations.py` | 6 | Not hooked: `filter_existing_duplicate_of_edges` |
| `search/search_filters.py` | builder | Emits Cypher `WHERE` fragments from `SearchFilters` (dates, labels, edge types) |
| `graph_queries.py` | DDL | Index and full-text DDL per provider |

**How search is built.** [`search/search.py`](https://github.com/getzep/graphiti/blob/v0.31.2/graphiti_core/search/search.py) runs the methods a `SearchConfig` names (`cosine_similarity`, `bm25`, `breadth_first_search`, per [`search_config.py`](https://github.com/getzep/graphiti/blob/v0.31.2/graphiti_core/search/search_config.py)) for edges, nodes, episodes and communities, each a function in `search_utils.py` that hits the database. Rerankers run in Python (`rrf`, `mmr`, `cross_encoder` through a cross-encoder client), except `node_distance` and `episode_mentions`, which query the database and are hooked.

**How time is built.** `add_episode` extracts edges with the LLM, then [`utils/maintenance/edge_operations.py`](https://github.com/getzep/graphiti/blob/v0.31.2/graphiti_core/utils/maintenance/edge_operations.py) `resolve_extracted_edges` fetches candidates (`get_edge_invalidation_candidates`, unhooked Cypher), asks the LLM which ones the new fact contradicts, and `_apply_temporal_invalidation` sets `invalid_at`/`expired_at` by comparing dates in Python. Only the candidate fetch touches storage.

**Tests.** `tests/helpers_test.py` builds the driver matrix from `GraphProvider`, and `test_graphiti_int.py`, `test_node_int.py` and `test_edge_int.py` run against every driver in it. Adding Postgres to that matrix gives the fork Graphiti's own integration tests.

### The fork

`cbu-capstone-design-27/graphiti`, branch `ember`, on top of the upstream tag.

**1. Postgres driver** (`graphiti_core/driver/postgres_driver.py` and `driver/postgres/`, asyncpg):

- `PostgresDriver(GraphDriver)`, `provider = GraphProvider.POSTGRES`. `execute_query` raises an error naming the caller, so any Cypher fallback the hooks miss fails a test instead of running.
- `graph_operations_interface` and `search_interface` implemented in SQL over the Part 1 tables, so core never falls back. Then the 11 new operation ABCs for the namespaces.
- `session()`/`transaction()` can wrap a connection the caller passes in, so Ember's load commits Graphiti's writes and `episodes.loaded_at` together.
- `build_indices_and_constraints()` does nothing: dbmate migrations own the schema, as for every other Ember table.

| Graphiti model | Postgres |
|---|---|
| `EntityNode` (`uuid`, `name`, `group_id`, `labels`, `summary`, `name_embedding`, `attributes`) | `graph.nodes`: `group_id` = `tenant || '_' || source`; `labels` = `['Entity', label]`; `attributes` = `props` |
| `EntityEdge` (`RELATES_TO`: `name`, `fact`, `fact_embedding`, `episodes`, `valid_at`, `invalid_at`, `expired_at`) | `graph.edges`: `name` = `type`; `episodes` from `episode_id` and `graph.mentions` |
| `EpisodicNode` | The existing `episodes` table: `content` = `text`, `valid_at` = `occurred_at` |
| `EpisodicEdge` (`MENTIONS`) | `graph.mentions` |
| `CommunityNode`, `SagaNode` and their edges | Not stored. Methods return nothing; Ember does not call `build_communities` or pass `saga` |

| Search method | SQL |
|---|---|
| `bm25` (nodes, edges, episodes) | `search @@ websearch_to_tsquery('simple', q)`, ranked by `ts_rank_cd`. Not true BM25, but rank fusion only uses order |
| `cosine_similarity` | `embedding <=> $q` / `fact_embedding <=> $q` on HNSW, `hnsw.iterative_scan` for the `group_id` filter |
| `breadth_first_search` | Recursive CTE over `graph.edges`, depth-limited, current edges only |
| `node_distance` reranker | The same CTE, returning hop counts from the centre node |
| `episode_mentions` reranker | `count(*)` from `graph.mentions` |
| `SearchFilters` | A SQL translation of the same filter object (dates, labels, edge types) |

**2. Core changes** (small, and each one upstreamable):

- Add `POSTGRES` to `GraphProvider`.
- Hook the seven unhooked call sites into `search_interface` or `graph_operations_interface`, the same try-then-fallback pattern core already uses.
- Let `search_filters.py` hand the driver the `SearchFilters` object instead of only Cypher fragments.
- Add Postgres to `tests/helpers_test.py`.

**3. What stays as is:** `add_episode`, the extraction prompts, entity resolution and dedupe, `resolve_extracted_edges` and `_apply_temporal_invalidation`, `search()` with its configs and recipes, RRF, MMR and cross-encoder rerankers, the LLM, embedder and cross-encoder clients.

### Ember on the fork

- **Subgraphs.** `group_id` = `ontology.subgraph_id(tenant, source)`. Search takes `group_ids=ontology.tenant_subgraphs(tenant)`, plus `{tenant}_code`.
- **Ontology.** `ENTITY_TYPES`, `EDGE_TYPES` and `EDGE_TYPE_MAP` in `services/pipeline/ontology` already have the shapes `add_episode(entity_types=, edge_types=, edge_type_map=)` takes. ADR 0002 wrote them for this.
- **Structured records, no LLM.** Load turns an episode's `attrs` (EMBER-85's deterministic mapping) into `EntityNode`s and `EntityEdge`s with uuid5 ids and calls `add_nodes_and_edges_bulk` through the driver, inside its own transaction. Part 1's ordering, delete, placeholder and invalidation rules run in load before the call. `fact` is generated from a template per edge type ("{src} is assigned to {dst}").
- **Free text, with the LLM.** Slack messages and comment bodies also go to `add_episode` with a narrowed ontology (`Decision`, `Module`, and `REFERENCES`/`DECIDED_IN`/`AFFECTS`/`SUPERSEDES`), so Graphiti extracts decisions and resolves them against existing nodes. Its contradiction check then invalidates superseded decision facts. This is the only place Ember pays for LLM calls.
- **Links.** Part 1's link step (`SAME_AS` by email, keys, paths, symbols) writes through the same bulk call.
- **Code.** The code subgraph's records arrive like any other source and use the structured path.
- **Search.** `graphiti.search_(query, config=COMBINED_HYBRID_SEARCH_RRF, group_ids=...)`, with `center_node_uuid` set to the top hit for a node-distance pass when the agent asks about one object.

### MCP tools

- `search(question, k, sources)` keeps its arguments and its `Hit` shape. It runs today's `source_records` vector search and Graphiti's hybrid search, fuses both lists by reciprocal rank, and adds `related` to each hit: up to five facts (edge `fact` strings with `valid_at`) around the hit's node.
- `get_record(source, object_id)` adds `node` (label, key, name, summary) and `facts` (current 1-hop edges), read through `graphiti.nodes` and `graphiti.edges`.
- One new tool, `history(source, object_id)`: every fact about the object, including invalidated ones, with the episode that set or ended it.
- All three stay read-only and run under the tenant's row-level security role.

### Upstream

- **Tracking.** Keep our changes as three commits on `ember`: core hooks, Postgres driver, tests. On each upstream release, rebase `ember` onto the new tag, run Graphiti's integration tests on Postgres and Ember's e2e-check, then bump the pinned tag in `services/pipeline/requirements.txt` (`graphiti-core @ git+https://github.com/cbu-capstone-design-27/graphiti@ember-v0.31.2`). The hooks touch files upstream edits often (`search_utils.py`, `graphiti.py`), so expect small conflicts each rebase.
- **Upstreaming.** Offer the core hooks first: they help every driver and need no Postgres knowledge from the maintainers. Offer the driver as a `graphiti-core[postgres]` extra after it passes their test matrix. Postgres requests ([#779](https://github.com/getzep/graphiti/issues/779), [#1095](https://github.com/getzep/graphiti/issues/1095)) were closed without a backend, so ask in an issue before opening the PR.

### Code graph

#### What graphify does

[graphify](https://github.com/Graphify-Labs/graphify) (PyPI `graphifyy` 0.9.97, 2026-10-27, Apache-2.0) parses about 40 languages with tree-sitter, locally and with no LLM, and optionally runs an LLM pass over docs, PDFs and media. Its output is `graphify-out/graph.json` (networkx node-link format), `graph.html` and `GRAPH_REPORT.md`. Every extractor returns ([ARCHITECTURE.md](https://github.com/Graphify-Labs/graphify/blob/v8/ARCHITECTURE.md)):

```json
{"nodes": [{"id": "...", "label": "human name", "source_file": "path", "source_location": "L42"}],
 "edges": [{"source": "id_a", "target": "id_b", "relation": "calls|imports|uses|...",
            "confidence": "EXTRACTED|INFERRED|AMBIGUOUS"}]}
```

Run with `graphify update .` (AST only) on `services/pipeline` and `apps/mcp` of this repo, it built 1,408 nodes and 3,842 edges in 1.5 s:

- Nodes by `file_type`: code 1,006, document 183 (Markdown headings), rationale 175 (docstrings and `# NOTE`/`# WHY` comments), concept 44. Extra fields: `community` (Leiden), `norm_label`, `_callable_class`.
- Edges by `relation`: calls 1,475, contains 590, imports 503, method 464, imports_from 291, rationale_for 175, references 133, uses 129, inherits 47, indirect_call 27, re_exports 8. 3,562 `EXTRACTED`, 280 `INFERRED`.
- Ids are path-derived strings (`services_pipeline_loader_check_rationale_1`). There is no explicit node kind for file, class or function, and no commit or time.

Our clone only needs the tree-sitter pass, with explicit kinds and keys that join to the other sources.

#### `{tenant}_code` in the same tables

| Code node | Label | Key | From graphify |
|---|---|---|---|
| Repository | `Container` (exists) | `github:org/repo`, the GitHub Container's key | n/a |
| Directory or package | `Module` (exists) | `org/repo:services/pipeline/loader` | `contains` from directories |
| File | `File` (new) | `org/repo:services/pipeline/loader/extract.py` | `source_file` |
| Class, function, method | `Symbol` (new), `props.kind` | `org/repo:services/pipeline/loader/extract.py::extract` | code nodes |

| Code edge | Type | From graphify |
|---|---|---|
| Module → File, File → Symbol, class → method | `CONTAINS` (exists, new pairs) | `contains`, `method` |
| Symbol → Symbol | `CALLS` (new) | `calls`, `indirect_call` (`props.confidence`) |
| File → File or Module | `IMPORTS` (new) | `imports`, `imports_from`, `re_exports` |
| Symbol → Symbol | `INHERITS` (new) | `inherits`, `mixes_in` |

Rationale comments become `props.doc` on their symbol. Communities and god nodes are skipped: they are views, not facts.

**Ingest.** A push to the default branch already reaches `enrich:github`, which holds the only GitHub credential. It fetches the changed files at the pushed SHA, parses them, and writes one `source_records` row per file (`source = 'code'`, `kind = 'file'`, `object_id = 'org/repo:path'`, `version = SHA`). Extract maps it deterministically, and load writes it through the fork's bulk path like any other structured record. A symbol missing from a file's new version gets `invalid_at` = the commit time, so "what did `extract()` call last month" works the same way as every other time query. A first import of a repository is a reconcile over the tree at `HEAD`.

**Cross-subgraph links** (source `link`, made by the link step):

| Link | How it is found |
|---|---|
| PR touches a file | Enrich already stores the PR's changed files; `Change -TOUCHES-> File` by exact `org/repo:path` |
| Ticket names a module | A repo path in Jira text (`services/pipeline/loader`) → `WorkItem -REFERENCES-> Module` by exact key |
| Slack thread about a function | An identifier in backticks or a `path::name` in a message → `Message -REFERENCES-> Symbol` when exactly one symbol in the tenant has that name |
| Commit touches a symbol | Changed line ranges of a commit against the symbol's `source_location` range → `Change -TOUCHES-> Symbol` |

Ambiguous names make no edge. The LLM pass can add the rest later, marked with its confidence.

## Effort

One developer who knows `services/pipeline`. Days are working days.

| Phase | Work | Days |
|---|---|---|
| A. Postgres store | Migration for `graph.*`; load's writer ported from `neo4j_writer.py` (ordering rule, tombstones, placeholders, mentions); edge invalidation per mapper; link step; e2e-check in SQL; deploy extract and load | 9 to 11 |
| B. Fork and driver | Core hooks and enum; `PostgresDriver` with the two legacy interfaces (97 methods, many one query each) and the 11 operation ABCs; Graphiti's integration tests on Postgres | 15 to 20 |
| C. Ember on the fork | Bulk path from episode `attrs`; fact templates; `add_episode` for free text with the narrowed ontology; MCP `search`, `get_record`, `history`; retrieval suite with and without the graph | 7 to 9 |
| D. Code graph | Tree-sitter extractor for Python and TypeScript; `code` kind in `enrich:github`; mapper; the four cross links | 10 to 15 |

A comes first and stands alone. B and C replace what Neo4j and Graphiti were for. D can follow either.

## Risks

- **Graphiti's storage layer is mid-migration.** Core has two driver abstractions (the legacy hooks and the new operations) and still runs Cypher directly in seven places. Our driver is the first to rely on the hooks, so we will find gaps, and upstream may move them under us. Rebases will conflict in `search_utils.py` and `graphiti.py`.
- **Structured invalidation is still ours.** The bulk path skips Graphiti's LLM contradiction check, so the per-mapper rules in Part 1 (which edges a record lists in full) need tests per source.
- **Two writers of the same tables.** Load's bulk path and `add_episode` both write `graph.*`. The per-subgraph advisory lock must cover both.
- **Hubs in walks.** A project's `CONTAINS` edges reach every ticket. Walks skip container edges and cap depth; the benchmark's 3-hop query shows the size of the problem (146 nodes from one ticket).
- **One database is one failure domain.** CNPG runs one instance on local storage, and `cluster.yaml` has no backup configured. That is true today for everything else in Ember; moving the graph in raises the stakes but not the work.
- **Ontology changes.** `SAME_AS`, `File`, `Symbol`, `CALLS`, `IMPORTS` and `INHERITS`, and new `CONTAINS`/`TOUCHES`/`REFERENCES` pairs, need a schema decision ([`docs/graph-schema.md`](../graph-schema.md), Extending).
- **The graphify clone can grow without end.** Upstream supports about 40 languages. Two cover Ember.
- **Neo4j work is retired.** `neo4j_writer.py`, `schema.cypher`, the Neo4j HelmRelease and the Neo4j half of the benchmarks go away once phase A lands. That is Brandon's code; the decision needs him.
- **LLM cost on free text.** `add_episode` makes several LLM calls per message. Narrowing the ontology and sending only messages longer than a threshold keeps it bounded; the local model's structured output is ADR 0002's open risk.

## Open questions for the team

1. Do we add `SAME_AS` (Person → Person) to the ontology, or merge people into one tenant-level `Person` with per-source `Identity` nodes?
2. Does ADR 0002 get superseded by a new ADR for "graph in Postgres, Graphiti fork with a Postgres driver"? This doc is the input, not the decision.
3. How long are `episodes` kept? Replays and node history read them.
4. Which free-text records go through `add_episode`: every Slack message and comment, or only those over a length threshold?
5. Is Python plus TypeScript enough for the first code graph, and is the default branch enough, or do open PR branches need their own code state?
6. Do we ask Zep before forking whether they would take a Postgres driver upstream?

## Sources

Fetched 2026-10-28.

- PostgreSQL: [19 Beta 1](https://www.postgresql.org/about/news/postgresql-19-beta-1-released-3313/) (2026-06-04, lists SQL/PGQ), [19 Beta 4](https://www.postgresql.org/about/news/postgresql-19-beta-4-released-3386/) (2026-09-24, "Revert SQL/PGQ (property graph query) support"), [19 RC 1](https://www.postgresql.org/about/news/postgresql-19-rc-1-released-3409/) (2026-10-15, GA planned 2026-11-19), [19 release notes](https://www.postgresql.org/docs/19/release-19.html) (no property graph entry), [SQL/PGQ commit as announced](https://www.depesz.com/2026/07/31/waiting-for-postgresql-19-sql-property-graph-queries-sql-pgq/).
- Apache AGE: [release notes](https://age.apache.org/release-notes/) (1.7.0 for PG18, 2026-01-21), [GitHub releases](https://github.com/apache/age/releases) (`PG18/v1.8.0-rc0`, 2026-07-08), [EDB 1.8.0 notes](https://www.enterprisedb.com/docs/pg_extensions/apache_age/rel_notes/apache_age_1.8.0_rel_notes/) (2026-08-11, PG18 only), [roadmap discussion #2305](https://github.com/apache/age/discussions/2305).
- CloudNativePG: [image volume extensions](https://cloudnative-pg.io/docs/current/imagevolume_extensions/) (PG18+), [extension images](https://github.com/cloudnative-pg/postgres-extensions-containers) (no AGE).
- pgvector: [iterative index scans](https://github.com/pgvector/pgvector#iterative-index-scans).
- Graphiti: [v0.31.2 source](https://github.com/getzep/graphiti/tree/v0.31.2) (commit `d72407d`, read 2026-10-29: `driver/`, `search/`, `nodes.py`, `edges.py`, `utils/bulk_utils.py`, `utils/maintenance/edge_operations.py`, `tests/helpers_test.py`), [README](https://github.com/getzep/graphiti) (drivers), [search config](https://github.com/getzep/graphiti/blob/main/graphiti_core/search/search_config.py) (methods and rerankers), [graphiti-core on PyPI](https://pypi.org/project/graphiti-core/) (0.31.2, 2026-10-20), issues [#779](https://github.com/getzep/graphiti/issues/779) and [#1095](https://github.com/getzep/graphiti/issues/1095).
- graphify: [README](https://github.com/Graphify-Labs/graphify), [ARCHITECTURE.md](https://github.com/Graphify-Labs/graphify/blob/v8/ARCHITECTURE.md), [graphifyy on PyPI](https://pypi.org/project/graphifyy/) (0.9.97, 2026-10-27).

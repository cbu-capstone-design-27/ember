# Postgres as Ember's graph store

Jira: EMBER-117 (Postgres graph store), EMBER-118 (our own Graphiti on Postgres). Date: 2026-10-28. Status: research, for a team decision. If accepted it supersedes [ADR 0002](../adr/0002-graph-engine-and-schema-approach.md)'s choice of Neo4j and Graphiti, not its ontology.

Diagrams (open at [excalidraw.com](https://excalidraw.com) with Open): [`ember-ingestion-to-graph`](diagrams/ember-ingestion-to-graph.excalidraw), [`ember-postgres-subgraphs`](diagrams/ember-postgres-subgraphs.excalidraw), [`ember-graphiti-postgres`](diagrams/ember-graphiti-postgres.excalidraw), [`ember-graph-options`](diagrams/ember-graph-options.excalidraw). Benchmark script: [`postgres-graph-bench.py`](postgres-graph-bench.py).

## Recommendation

Store the graph in the Postgres we already run, as two plain tables (`graph.nodes`, `graph.edges`) with time columns on edges, queried with joins and recursive CTEs. Do not use Apache AGE, and do not wait for SQL/PGQ: AGE needs a custom CloudNativePG image and was the slowest engine here, and SQL/PGQ was pulled from PostgreSQL 19 before release. Plain tables answered every benchmark query in under 3 ms at 100 tenants, fastest or tied on all four, put vector search and graph expansion in one SQL statement, and remove Neo4j as a second database to run, back up and keep in step.

Most of the pipeline already exists. `extract` (EMBER-85) maps each `source_records` row to an `episodes` row holding the ontology's nodes and edges. Only the `load` stage changes: it writes those episodes into the graph tables instead of Neo4j, in the same transaction that marks them loaded. The rest of Graphiti that Ember needs (episodes, temporal edges, hybrid search with rank fusion and graph-distance rerank) is a small Python package over these tables. LLM extraction is needed only for free text, and only later.

Effort: about 2 weeks for the Postgres store, 1.5 weeks for search and the MCP tools, 2 to 3 weeks for the code graph, 1.5 weeks for LLM extraction of decisions. Details in [Effort](#effort).

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

Graphiti itself is not an option on Postgres: graphiti-core 0.31.2 (2026-10-20) supports Neo4j 5.26, FalkorDB, Amazon Neptune and Kuzu (deprecated). Requests for a Postgres backend ([#779](https://github.com/getzep/graphiti/issues/779), pgvector; [#1095](https://github.com/getzep/graphiti/issues/1095), AGE) were closed without one.

### Benchmark

Photon as `extract` maps it (1,140 nodes, 2,327 edges, 666 messages with vectors), copied once per tenant. The Parallax GitHub load had 2,391 nodes and about 11,000 edges ([`neo4j-load-check.md`](../pipeline/neo4j-load-check.md#github-the-parallax-repository-ember-107)), so one tenant is photon-to-Parallax size and 100 tenants is 114,000 nodes, 232,700 edges and 66,600 random 1024-dim vectors. Postgres 18.6 with pgvector 0.8.7 and AGE 1.8.0-rc0 in one scratch container, Neo4j 5.26.32 Community (the compose image) in another, on an Apple M3 Pro under Docker Desktop. Each query runs 30 times after one warm-up, from Python on the same host, with a random tenant and start node. All engines returned the same answers. Median / p95 ms:

| Query | Postgres tables, 1 / 100 tenants | AGE 1.8, 1 / 100 | Neo4j 5.26, 1 / 100 |
|---|---|---|---|
| 1-hop neighbours of a ticket | 1.0 / 0.8 | 24.2 / **524.3** | 4.4 / 4.5 |
| Co-participants of a person (Person→Message→Conversation←Message←Person) | 1.3 / 2.8 | 0.5 / 2.8 | 4.1 / 6.8 |
| Everything within 3 hops of a ticket | 1.5 / 1.5 | 5.1 / 4.7 | 5.2 / 4.9 |
| Vector top-10 messages, with thread and author | 2.5 / 2.6 | n/a | 5.8 / **47.7** |

- Every query is a few milliseconds on plain tables at both sizes, because a walk only touches its neighbourhood and every edge lookup is an index range scan. Size does not matter at Ember's scale. Hubs do: a 3-hop walk from a ticket reaches 146 nodes through its project's `CONTAINS` edges.
- AGE's untyped `-[]-` scans every edge label table. Typed patterns were fine.
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
  tenant      text not null,
  source      text not null,          -- github | gitlab | jira | slack | teams | code
  label       text not null,          -- ontology ENTITY_TYPES (plus File and Symbol for code)
  key         text not null,          -- the mapper's key; external_id for source-backed nodes
  name        text,
  props       jsonb not null default '{}',
  placeholder boolean not null default false,   -- created by an edge before its node's own episode
  deleted     boolean not null default false,
  occurred_at timestamptz,            -- source time of the newest version applied
  embed_text  text,
  embedding   vector(1024),           -- ADR 0001; written by the embed worker
  search      tsvector generated always as (to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(embed_text, ''))) stored,
  unique (tenant, source, label, key)
);
create index nodes_embedding on graph.nodes using hnsw (embedding vector_cosine_ops);
create index nodes_search on graph.nodes using gin (search);

-- One row per fact. valid_at/invalid_at: when it was true in the source. created_at/expired_at: when Ember held it.
create table graph.edges (
  id             bigint generated always as identity primary key,
  tenant         text not null,
  source         text not null,       -- the subgraph that asserted it; 'link' for cross-subgraph edges
  type           text not null,       -- ontology EDGE_TYPES
  src            bigint not null references graph.nodes on delete cascade,
  dst            bigint not null references graph.nodes on delete cascade,
  props          jsonb not null default '{}',   -- verdict, link_type, confidence, method
  valid_at       timestamptz not null,
  invalid_at     timestamptz,
  created_at     timestamptz not null default now(),
  expired_at     timestamptz,
  episode_id     bigint references episodes (id) on delete set null,   -- what asserted it
  invalidated_by bigint references episodes (id) on delete set null    -- what ended it: the "why"
);
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

`nodes.label` and `edges.type` stay free text, checked in code against the ontology the way `neo4j_writer.py` whitelists labels today, so a new type is a code change and not a migration. `EmberConfig` becomes one row in a `graph.config` table.

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

## Part 2 (EMBER-118): our Graphiti on Postgres

### What Graphiti does, and what Ember needs from it

| Graphiti | What it does | Ember's version |
|---|---|---|
| Episodes | Raw input with a reference time; every fact traces back to one | `episodes` exists. `graph.mentions` and `edges.episode_id` are the trace |
| Entity and edge extraction with an LLM | Extracts entities and facts from each episode against the ontology | Deterministic mapping for everything structured (done, EMBER-85). LLM only for free text: `Decision` nodes, `DECIDED_IN`, `AFFECTS`, `SUPERSEDES`, and `REFERENCES` that keys and paths miss. Runs in extract, writes the same episode `attrs` with `props.origin = 'llm'` and a confidence |
| Entity resolution | Embedding and LLM dedupe of extracted entities within a `group_id` | Source-backed nodes have exact keys, so no dedupe. People by email (above). LLM-extracted `Decision`s: nearest existing decision in the subgraph by embedding, merged above a threshold. `Module` by normalised path |
| Bi-temporal edges | `valid_at`, `invalid_at`, `created_at`, `expired_at`; contradicting facts invalidate old ones, decided by the LLM | Same four columns. Invalidation is deterministic from source versions; LLM contradiction checks only for decisions |
| Hybrid search | BM25, cosine and breadth-first search over nodes and edges | Postgres full text (`tsvector`, `ts_rank_cd`), pgvector cosine, and a depth-limited walk, in SQL. `ts_rank_cd` is not BM25; it only feeds rank fusion, which uses order, not scores. No BM25 extension ships in CNPG's images |
| Rerankers | RRF, node distance, episode mentions, MMR, cross-encoder | RRF, node distance and mention count. MMR and cross-encoder later, if the benchmark asks for them |
| Communities | Clusters with LLM summaries | Not needed |

### Package

`packages/ember-graph`, Python, asyncpg. The load worker and the MCP server both use it, which is the "second consumer" `apps/mcp/README.md` waits for before moving graph code to `packages/`.

```python
@dataclass(frozen=True)
class Subgraph:
    tenant: str
    source: str                     # "github" ... "code"; ontology.subgraph_id() for the string form

class Graph:
    def __init__(self, conn: asyncpg.Connection): ...

    # write: called by load, inside its transaction
    async def apply(self, episode: Episode) -> None: ...         # nodes, edges, mentions, invalidation, tombstones
    async def link(self, tenant: str, node_ids: list[int]) -> int: ...   # SAME_AS, cross-source REFERENCES
    async def drop(self, sub: Subgraph) -> None: ...             # replay and purge

    # read
    async def search(self, tenant: str, question: str, vector: list[float], *,
                     sources: list[str] | None = None, k: int = 10, hops: int = 1,
                     at: datetime | None = None) -> list[GraphHit]: ...
    async def neighbours(self, node_id: int, *, types: list[str] | None = None,
                         at: datetime | None = None) -> list[Fact]: ...
    async def history(self, node_id: int) -> list[Change]: ...   # every version and invalidation, with its episode

@dataclass(frozen=True)
class GraphHit:
    node_id: int
    source: str; label: str; key: str; name: str
    score: float
    facts: list[Fact]                # current edges around it, as "Ryan AUTHORED PHO-15"

@dataclass(frozen=True)
class Fact:
    type: str
    src: str; dst: str               # names
    valid_at: datetime
    invalid_at: datetime | None
    episode_key: str | None          # where it came from
```

`search` is one SQL statement:

1. `vec`: top 50 nodes by `embedding <=> :vector` in the tenant (and `sources`), not deleted, not placeholders.
2. `fts`: top 50 by `ts_rank_cd(search, websearch_to_tsquery('simple', :question))`.
3. Fuse by reciprocal rank, `1 / (60 + rank)`.
4. Expand the top 10 by `hops` over edges valid at `at` (now by default), skipping `CONTAINS` and `MEMBER_OF` so a project or channel does not pull in everything.
5. Rerank: fused score, plus a bonus by graph distance to the best hit, plus a small one for mention count.

`at` makes every query a point-in-time query. The benchmark's vector-plus-expansion query is steps 1 and 4 and took 2.6 ms at 100 tenants.

### MCP tools

- `search(question, k, sources)` keeps its arguments and its `Hit` shape. It runs today's `source_records` vector search and `Graph.search` with the same query vector, fuses both lists by reciprocal rank, and adds `related` to each hit: up to five facts around the hit's node. A hit from either list resolves to its `source_records` row for `url` and `snippet`.
- `get_record(source, object_id)` adds `node` (label, key, name) and `facts` (current 1-hop edges, with `valid_at`).
- One new tool, `history(source, object_id)`, returns `Graph.history`: what changed, when, and from which record. It is the "why did it change" question in ADR 0002.
- All three stay read-only (`default_transaction_read_only`) and run under the tenant's row-level security role.

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

**Ingest.** A push to the default branch already reaches `enrich:github`, which holds the only GitHub credential. It fetches the changed files at the pushed SHA, parses them, and writes one `source_records` row per file (`source = 'code'`, `kind = 'file'`, `object_id = 'org/repo:path'`, `version = SHA`). Extract maps it deterministically, and load applies it like any other episode. A symbol missing from a file's new version gets `invalid_at` = the commit time, so "what did `extract()` call last month" works the same way as every other time query. A first import of a repository is a reconcile over the tree at `HEAD`.

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
| A. Postgres store | Migration for `graph.*`; `PgGraphWriter` ported from `neo4j_writer.py` (ordering rule, tombstones, placeholders, mentions); edge invalidation per mapper; link step (email, keys); e2e-check in SQL; deploy extract and load | 9 to 11 |
| B. Search | `packages/ember-graph` search, neighbours, history; embed worker also embeds `graph.nodes`; MCP `search`, `get_record`, `history`; the retrieval suite in `docs/benchmarks.md` with and without the graph | 7 to 8 |
| C. Code graph | Tree-sitter extractor for Python and TypeScript (Ember's own languages); `code` kind in `enrich:github`; mapper; the four cross links | 10 to 15 |
| D. LLM extraction | Decisions and missed references from free text, in extract, through `services/pipeline/models` | 7 to 8 |

A and B replace what Graphiti and Neo4j were for. C and D can follow in either order.

## Risks

- **We own the temporal logic.** Graphiti would have given us invalidation; ours is deterministic and testable, but its rules (which edges a record lists in full) are per mapper and need tests per source.
- **Hubs in walks.** A project's `CONTAINS` edges reach every ticket. Walks skip container edges and cap depth; the benchmark's 3-hop query shows the size of the problem (146 nodes from one ticket).
- **One database is one failure domain.** CNPG runs one instance on local storage, and `cluster.yaml` has no backup configured. That is true today for everything else in Ember; moving the graph in raises the stakes but not the work.
- **Ontology changes.** `SAME_AS`, `File`, `Symbol`, `CALLS`, `IMPORTS` and `INHERITS`, and new `CONTAINS`/`TOUCHES`/`REFERENCES` pairs, need a schema decision ([`docs/graph-schema.md`](../graph-schema.md), Extending).
- **The graphify clone can grow without end.** Upstream supports about 40 languages. Two cover Ember.
- **Neo4j work is retired.** `neo4j_writer.py`, `schema.cypher`, the Neo4j HelmRelease and the Neo4j half of the benchmarks go away once phase A lands. That is Brandon's code; the decision needs him.

## Open questions for the team

1. Do we add `SAME_AS` (Person → Person) to the ontology, or merge people into one tenant-level `Person` with per-source `Identity` nodes?
2. Does ADR 0002 get superseded by a new ADR for "graph in Postgres, Graphiti-like package of our own"? This doc is the input, not the decision.
3. How long are `episodes` kept? Replays and node history read them.
4. Which free-text facts justify the LLM pass: decisions only, or also references the key and path rules miss?
5. Is Python plus TypeScript enough for the first code graph, and is the default branch enough, or do open PR branches need their own code state?

## Sources

Fetched 2026-10-28.

- PostgreSQL: [19 Beta 1](https://www.postgresql.org/about/news/postgresql-19-beta-1-released-3313/) (2026-06-04, lists SQL/PGQ), [19 Beta 4](https://www.postgresql.org/about/news/postgresql-19-beta-4-released-3386/) (2026-09-24, "Revert SQL/PGQ (property graph query) support"), [19 RC 1](https://www.postgresql.org/about/news/postgresql-19-rc-1-released-3409/) (2026-10-15, GA planned 2026-11-19), [19 release notes](https://www.postgresql.org/docs/19/release-19.html) (no property graph entry), [SQL/PGQ commit as announced](https://www.depesz.com/2026/07/31/waiting-for-postgresql-19-sql-property-graph-queries-sql-pgq/).
- Apache AGE: [release notes](https://age.apache.org/release-notes/) (1.7.0 for PG18, 2026-01-21), [GitHub releases](https://github.com/apache/age/releases) (`PG18/v1.8.0-rc0`, 2026-07-08), [EDB 1.8.0 notes](https://www.enterprisedb.com/docs/pg_extensions/apache_age/rel_notes/apache_age_1.8.0_rel_notes/) (2026-08-11, PG18 only), [roadmap discussion #2305](https://github.com/apache/age/discussions/2305).
- CloudNativePG: [image volume extensions](https://cloudnative-pg.io/docs/current/imagevolume_extensions/) (PG18+), [extension images](https://github.com/cloudnative-pg/postgres-extensions-containers) (no AGE).
- pgvector: [iterative index scans](https://github.com/pgvector/pgvector#iterative-index-scans).
- Graphiti: [README](https://github.com/getzep/graphiti) (drivers), [search config](https://github.com/getzep/graphiti/blob/main/graphiti_core/search/search_config.py) (methods and rerankers), [graphiti-core on PyPI](https://pypi.org/project/graphiti-core/) (0.31.2, 2026-10-20), issues [#779](https://github.com/getzep/graphiti/issues/779) and [#1095](https://github.com/getzep/graphiti/issues/1095).
- graphify: [README](https://github.com/Graphify-Labs/graphify), [ARCHITECTURE.md](https://github.com/Graphify-Labs/graphify/blob/v8/ARCHITECTURE.md), [graphifyy on PyPI](https://pypi.org/project/graphifyy/) (0.9.97, 2026-10-27).

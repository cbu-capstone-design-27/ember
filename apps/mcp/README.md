# apps/mcp

MCP (Model Context Protocol) server that lets a coding agent (Claude Code, Codex) search the GitHub, Jira, Slack and GitLab history Ember has ingested into `source_records` (EMBER-79), and the Postgres graph built from it, through the Graphiti fork (EMBER-166): who is assigned to what, which threads reference which tickets, and how that changed. It runs on stdio and only reads.

## Who it reads as (EMBER-166)

The server reads for one Ember user, `EMBER_USER_EMAIL`, and sees every graph and record of every tenant that user belongs to: each organization's tenant, and their personal workspace's, which is an organization too (EMBER-165). Nothing else.

- **Tenants.** On each tool call it runs `ember_user_tenants(email)`, which reads the web app's Better Auth tables (`web.user` → `web.member` → `web.organization.tenant`, matched on the lowercased email). The function is `security definer`, owned by `ember`. That is simpler than column grants: the web app creates `web.*` on its first start, after the migrations, so a migration can't grant on them, and the reader never gets a direct view of other users' memberships. The function is plpgsql, so it finds the tables at call time and returns no tenants before they exist.
- **Row-level security.** Every call is one read-only transaction that sets `ember.tenants` to that list (`set_config(..., true)`). The policies from EMBER-164 do the rest on `graph.*`, `episodes` and `source_records`. Graphiti runs on the same connection.
- **The role.** `DATABASE_URL` must be a login role in `ember_graph_reader`, `ember_mcp`. The server refuses to run as the tables' owner, a superuser or a role with `bypassrls`, since those skip row-level security. Locally, compose's `postgres-mcp-role` creates it (password `MCP_POSTGRES_PASSWORD`, default `change-me-locally`). On the cluster, the operator manages it (`infra/k8s/apps/postgres/cluster.yaml`), with its password in Secret `postgres-mcp`.
- **No access.** An email in no organization gets a tool error from `search`, `get_record` and `history`, and `whoami` shows no tenants.

## Search

`search` fuses two ranked lists by reciprocal rank (k = 60), then keeps the newest version of each object:

1. **Records by meaning**, only when `EMBEDDING_URL` is set (EMBER-78). The question is embedded with the query template `Instruct: {task}\nQuery: {question}` ([ADR 0001](../../docs/adr/0001-embedding-model.md), [`services/embedding`](../../services/embedding/README.md)) and matched against `source_records.embedding` on its HNSW index. Only rows the embed worker has processed are searchable.
2. **The graph**, through the Graphiti fork (`graphiti-core @ ember-v0.31.2`, `PostgresDriver`): `search_` with `COMBINED_HYBRID_SEARCH_RRF` (BM25 and cosine over nodes and facts, BM25 over episodes) and the same query vector. Without `EMBEDDING_URL` it uses BM25 plus a breadth-first walk from what BM25 found, and `whoami` says so. Graphiti's nodes, facts and episodes map back to the source records behind them. A record's node is the first `WorkItem`, `Change` or `Message` in its episode's `attrs.nodes`.

Each hit gets `related`: up to five facts that hold now around its node, newest first. With no `sources` or `graphs` filter, Graphiti searches with `group_ids` unset, so row-level security limits it to the user's graphs and the link edges between them (`SAME_AS`, `REFERENCES` across graphs, whose `graph_id` is null) are included. With a filter it passes those graphs' `group_id`s, which leaves out link edges.

The embed worker ([`services/embed`](../../services/embed/README.md)) also embeds `graph.nodes.embed_text` and `graph.edges.fact` with the same model, so graph cosine search works once it has run.

## Run

Python 3.12. From the environment only:

| Variable | |
|---|---|
| `DATABASE_URL` | Postgres as `ember_mcp`, e.g. `postgresql://ember_mcp:change-me-locally@localhost:5432/ember?sslmode=disable` |
| `EMBER_USER_EMAIL` | The Ember account to read as |
| `EMBEDDING_URL` | Optional. Base URL, without `/v1`. Unset: BM25 and graph traversal only |
| `EMBEDDING_API_KEY` | Optional, sent as `Authorization: Bearer` only when set |
| `EMBEDDING_MODEL` | Optional, default `qwen3-embedding-0.6b` |

Every transaction is read-only (`default_transaction_read_only`). A missing variable, an unreachable database or embedding service, or a role that isn't a reader comes back as a tool error, and the server keeps running.

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r apps/mcp/requirements.txt
cd apps/mcp
DATABASE_URL='postgresql://ember_mcp:change-me-locally@localhost:5432/ember?sslmode=disable' \
EMBER_USER_EMAIL=you@example.com ../../.venv/bin/python -m ember_mcp
```

To fill the local database, see [Local stack](#local-stack). `seed_photon.py` still inserts the Photon synthetic Jira and Slack records (as `ember`, the owner: it writes). Without `--no-embed` it also embeds them through the embed worker, which needs `EMBEDDING_URL`:

```sh
DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
  .venv/bin/python apps/mcp/seed_photon.py --no-embed
```

## Add it to an agent

Run these from the repo root. `PYTHONPATH` lets `python -m ember_mcp` find the package from any directory.

Claude Code:

```sh
claude mcp add ember \
  -e DATABASE_URL='postgresql://ember_mcp:change-me-locally@localhost:5432/ember?sslmode=disable' \
  -e EMBER_USER_EMAIL=you@example.com \
  -e PYTHONPATH="$PWD/apps/mcp" \
  -- "$PWD/.venv/bin/python" -m ember_mcp
```

Add `-e EMBEDDING_URL=... -e EMBEDDING_API_KEY=...` for vector search.

Codex:

```sh
codex mcp add ember \
  --env DATABASE_URL='postgresql://ember_mcp:change-me-locally@localhost:5432/ember?sslmode=disable' \
  --env EMBER_USER_EMAIL=you@example.com \
  --env PYTHONPATH="$PWD/apps/mcp" \
  -- "$PWD/.venv/bin/python" -m ember_mcp
```

## Tools

All four are annotated `readOnlyHint: true`.

### `search`

| Argument | Type | Default | |
|---|---|---|---|
| `question` | string | required | A natural-language question or description |
| `k` | integer | 8 | Hits to return, 1 to 50 |
| `sources` | string[] or null | null (all) | Any of `github`, `jira`, `slack`, `gitlab` |
| `graphs` | string[] or null | null (all) | Graph names, scopes or `group_id`s, case-insensitive: `org/repo`, `site.atlassian.net/KEY`, a Slack team id. `whoami` lists them |

Returns `{"result": [Hit, ...]}`, best first. Search covers only the newest version of each object and skips objects whose newest version is a delete.

| Hit field | |
|---|---|
| `source`, `kind`, `object_id`, `version`, `occurred_at` | The `source_records` row |
| `title` | PR or issue title (Jira: `KEY summary`), first line of a commit, or the start of a message |
| `snippet` | The first 300 characters of the record's `embed_text`, or of its graph node's when the record isn't embedded |
| `url` | GitHub `html_url`, the Jira browse URL built from the site and key, the GitLab `url`, or a Slack permalink if stored. Otherwise null |
| `score` | The fused score, the sum of `1 / (60 + rank)` over the lists that found it |
| `related` | Up to five current facts around the hit's node: `type`, `fact` ("Eli is assigned to Rotate the signing key"), `valid_at` |

### `get_record`

| Argument | Type | |
|---|---|---|
| `source` | string | A hit's `source` |
| `object_id` | string | A hit's `object_id` |

Returns the newest version of that object: `source`, `tenant`, `kind`, `object_id`, `version`, `occurred_at` and the full `record` JSON, plus `node` (`label`, `key`, `name`, `summary`, read through Graphiti's `EntityNode`), `facts` (the current 1-hop facts, through `EntityEdge.get_by_node_uuid`) and `graph` (`name`, `scope`, `source`). `node` and `graph` are null for a record that isn't loaded yet. A record over 50,000 characters comes back as truncated JSON text with `"truncated": true`. An object the user can't see is a tool error.

### `history`

Same arguments as `get_record`. Returns every fact about the object's node, ended ones included, oldest first: `type`, `fact`, `valid_at`, `invalid_at` (null while it holds), `set_by` and `ended_by`. Each of those is the episode (`episode` key) and source record version (`source`, `object_id`, `version`) behind it, or null. Graphiti's `EntityEdge` has no field for the episode that ended a fact, so `ended_by` is read from `graph.edges.invalidated_by` in the same reader transaction.

### `whoami`

No arguments. Returns `email`, `tenants`, `graphs` (`id`, `tenant`, `source`, `scope`, `name`, `group_id`, `nodes`) and `search`, which says whether vector search is on.

## Tests

[`test_mcp.py`](test_mcp.py) runs against a real Postgres with pgvector built from the repo migrations, as the enrich tests do, and a stub embedding server that hashes words into 1024 dimensions, so texts sharing words are close. It connects as a reader role it creates (`ember_mcp_test`), with Better Auth tables for two users. The graph tests load records through the pipeline's extract and load (`GRAPH_STORE=postgres`'s writer). It also seeds the photon data with `seed_photon.py` and checks five hand-written questions find their records. So it needs `services/embed/requirements.txt` and `services/pipeline/requirements.txt` too. It is the `MCP server against Postgres` step in CI.

```sh
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 apps/mcp/test_mcp.py
```

## Conventions

- This package is the only place MCP protocol handling lives.
- Graph access goes through shared code in `packages/` once a second consumer exists. Until then it may live here.

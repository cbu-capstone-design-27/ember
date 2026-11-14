# apps/mcp

MCP (Model Context Protocol) server that lets a coding agent (Claude Code, Codex) search the GitHub, Jira, Slack and GitLab history Ember has ingested into `source_records` (EMBER-79). It runs on stdio and only reads.

## Search: by meaning

`search` is semantic vector search (EMBER-78). The question is embedded by the same embedding service the embed worker uses ([ADR 0001](../../docs/adr/0001-embedding-model.md), [`services/embedding`](../../services/embedding/README.md)), with the query template `Instruct: {task}\nQuery: {question}` and the README's default task. Documents were embedded without a prefix. Rows are ranked by cosine distance to `source_records.embedding` on its HNSW index (`order by embedding <=> $q limit k`). Only rows the embed worker ([`services/embed`](../../services/embed/README.md)) has processed are searchable.

Every backend implements one interface, `Search`: `(question, k, sources, tenant) -> list[Hit]` in [`ember_mcp/retrieval.py`](ember_mcp/retrieval.py), the interface agreed for EMBER-119. `tenant` None searches every tenant, and the tool passes None. There is no keyword fallback: if the embedding service is unreachable or returns a vector that isn't 1024-long, `search` is a tool error.

## Backends (EMBER-122)

`RETRIEVAL_BACKEND` picks the backend, read on every call. Any other value is a tool error.

| Value | |
|---|---|
| `pgvector` (default) | The vector search above, unchanged |
| `neo4j` | Seed and expand over Neo4j as `services/pipeline`'s load writes it. Reads `NEO4J_URI`, `NEO4J_USER` (default `neo4j`), `NEO4J_PASSWORD` and optionally `NEO4J_DATABASE`, in a read-only session |
| `pggraph` | Seed and expand over the `graph.*` tables of [ADR 0011](../../docs/adr/0011-graph-in-postgres.md) and [the research doc](../../docs/research/postgres-graph.md#tables), in Ember's Postgres. No migration creates them yet (research Phase A), so until one does, `search` is a tool error that says so |

Seed and expand, in [`ember_mcp/graph.py`](ember_mcp/graph.py), with no LLM:

1. The top `k` pgvector hits are the seeds.
2. Each seed's node is found through its `episodes` row (`source_record_id`): the record's own node is the node in `attrs.nodes` whose `props.kind` is the episode's kind. Load writes it in Neo4j on `(group_id = <tenant>_<source>, source, external_id = key)`, and in `graph.nodes` on `(tenant, source, label, key)`.
3. One hop from it, in either direction, over current edges. `CONTAINS` is skipped, because a project's or channel's `CONTAINS` edges reach every ticket or thread in it. Placeholder and deleted nodes are skipped.
4. A neighbour that is some record's own node becomes a hit, as that object's newest, non-deleted version, under the same source and tenant filters. People, statuses, threads and placeholders have no record, so they never become hits.

A graph hit scores `0.9 x` its seed's score (`GRAPH_WEIGHT`). A record reached twice keeps its best score. The top `k` by score come back, so graph hits can replace lower seeds. `get_record` reads `source_records` on every backend.

## Run

Python 3.12. `DATABASE_URL`, `EMBEDDING_URL` (base URL, without `/v1`), and optionally `EMBEDDING_API_KEY` (sent as `Authorization: Bearer` only when set) and `EMBEDDING_MODEL` (default `qwen3-embedding-0.6b`) come from the environment only. Every transaction the server opens is read-only (`default_transaction_read_only`). A missing variable, an unreachable database or an unreachable embedding service comes back as a tool error, and the server keeps running.

```sh
python3 -m venv .venv && .venv/bin/pip install -r apps/mcp/requirements.txt
cd apps/mcp
DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
EMBEDDING_URL=http://<spark>:8000 EMBEDDING_API_KEY=... ../../.venv/bin/python -m ember_mcp
```

To try it locally, start the compose Postgres, migrate it, and seed it with the Photon synthetic Jira and Slack data. The seed inserts the rows, then runs the embed worker's code (`services/embed`) until every row is embedded, so it needs that service's requirements and the embedding endpoint:

```sh
docker compose -f infra/docker-compose.yml --env-file .env.example --profile postgres up -d postgres
docker compose -f infra/docker-compose.yml --env-file .env.example --profile postgres run --rm postgres-migrate
.venv/bin/pip install -r services/embed/requirements.txt
DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
EMBEDDING_URL=http://<spark>:8000 EMBEDDING_API_KEY=... .venv/bin/python apps/mcp/seed_photon.py
```

Running `services/embed/embed.py` against the same database instead does the same, and keeps embedding new rows.

## Add it to an agent

Run these from the repo root. `PYTHONPATH` lets `python -m ember_mcp` find the package from any directory.

Claude Code:

```sh
claude mcp add ember \
  -e DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
  -e EMBEDDING_URL=http://<spark>:8000 -e EMBEDDING_API_KEY=... \
  -e PYTHONPATH="$PWD/apps/mcp" \
  -- "$PWD/.venv/bin/python" -m ember_mcp
```

Codex:

```sh
codex mcp add ember \
  --env DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
  --env EMBEDDING_URL=http://<spark>:8000 --env EMBEDDING_API_KEY=... \
  --env PYTHONPATH="$PWD/apps/mcp" \
  -- "$PWD/.venv/bin/python" -m ember_mcp
```

## Tools

Both tools are annotated `readOnlyHint: true`.

### `search`

| Argument | Type | Default | |
|---|---|---|---|
| `question` | string | required | A natural-language question or description |
| `k` | integer | 8 | Hits to return, 1 to 50 |
| `sources` | string[] or null | null (all) | Any of `github`, `jira`, `slack`, `gitlab` |

Returns `{"result": [Hit, ...]}`, best first. Search covers only the newest version of each object, skips objects whose newest version is a delete, and skips rows with no embedding yet.

| Hit field | |
|---|---|
| `source`, `kind`, `object_id`, `version`, `occurred_at` | The `source_records` row |
| `title` | PR or issue title (Jira: `KEY summary`), first line of a commit, or the start of a message |
| `snippet` | The first 300 characters of `embed_text`, the text that was embedded |
| `url` | GitHub `html_url`, the Jira browse URL built from the site and key, the GitLab `url`, or a Slack permalink if stored. Otherwise null |
| `score` | Cosine similarity, `1 - (embedding <=> question)`. A graph hit's is `0.9 x` its seed's |
| `via` | Graph backends only, on hits the graph added: `seed_source`, `seed_object_id` and `edge`, the edge type that links it to that seed |

### `get_record`

| Argument | Type | |
|---|---|---|
| `source` | string | A hit's `source` |
| `object_id` | string | A hit's `object_id` |

Returns the newest version of that object: `source`, `tenant`, `kind`, `object_id`, `version`, `occurred_at` and the full `record` JSON. A record over 50,000 characters comes back as truncated JSON text with `"truncated": true`. An unknown object is a tool error.

## Tests

[`test_mcp.py`](test_mcp.py) runs against a real Postgres with pgvector built from the repo migrations, as the enrich tests do, and a stub embedding server that hashes words into 1024 dimensions, so texts sharing words are close. It also seeds the photon data with `seed_photon.py` and checks five hand-written questions find their records, so it needs `services/embed/requirements.txt` too. It is the `MCP server against Postgres` step in CI.

The graph backend tests create the `graph.*` tables from the research prototype's `SCHEMA` and load a small fixture graph. CI has no Neo4j, so there the Neo4j driver call is stubbed. With `NEO4J_TEST_URI` and `NEO4J_PASSWORD` set, one more test loads the same graph into that Neo4j (deleting its `acme_*` nodes) and runs against it.

```sh
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 apps/mcp/test_mcp.py
```

## Conventions

- This package is the only place MCP protocol handling lives.
- Graph access goes through shared code in `packages/` once a second consumer exists. Until then it may live here.

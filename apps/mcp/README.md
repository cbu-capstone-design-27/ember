# apps/mcp

MCP (Model Context Protocol) server that lets a coding agent (Claude Code, Codex) search the GitHub, Jira, Slack and GitLab history Ember has ingested into `source_records` (EMBER-79). It runs on stdio and only reads.

## Search: full text now, vectors later

`search` is Postgres full-text search: `websearch_to_tsquery('english', question)` against the record's titles, bodies, descriptions, message text and commit messages, ranked by `ts_rank`. It needs no migration and no embedding model. Every word in the question must appear in a hit, so a message that says "the rewrite" without "Photon" won't match "Photon rewrite".

All retrieval sits behind one function, `search(question, k, sources) -> list[Hit]` in [`ember_mcp/retrieval.py`](ember_mcp/retrieval.py). Once pgvector embeddings (EMBER-77) land, EMBER-78 replaces that function's body with vector retrieval. `server.py` and the tool schemas don't change.

## Run

Python 3.12. `DATABASE_URL` comes from the environment only. Every transaction the server opens is read-only (`default_transaction_read_only`). A missing variable or an unreachable database comes back as a tool error, and the server keeps running.

```sh
python3 -m venv .venv && .venv/bin/pip install -r apps/mcp/requirements.txt
cd apps/mcp
DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' ../../.venv/bin/python -m ember_mcp
```

To try it locally, start the compose Postgres, migrate it, and seed it with the Photon synthetic Jira and Slack data:

```sh
docker compose -f infra/docker-compose.yml --env-file .env.example --profile postgres up -d postgres
docker compose -f infra/docker-compose.yml --env-file .env.example --profile postgres run --rm postgres-migrate
DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' .venv/bin/python apps/mcp/seed_photon.py
```

## Add it to an agent

Run these from the repo root. `PYTHONPATH` lets `python -m ember_mcp` find the package from any directory.

Claude Code:

```sh
claude mcp add ember \
  -e DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
  -e PYTHONPATH="$PWD/apps/mcp" \
  -- "$PWD/.venv/bin/python" -m ember_mcp
```

Codex:

```sh
codex mcp add ember \
  --env DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
  --env PYTHONPATH="$PWD/apps/mcp" \
  -- "$PWD/.venv/bin/python" -m ember_mcp
```

## Tools

Both tools are annotated `readOnlyHint: true`.

### `search`

| Argument | Type | Default | |
|---|---|---|---|
| `question` | string | required | Key words. Websearch syntax: `"quoted phrase"`, `-exclude`, `or` |
| `k` | integer | 8 | Hits to return, 1 to 50 |
| `sources` | string[] or null | null (all) | Any of `github`, `jira`, `slack`, `gitlab` |

Returns `{"result": [Hit, ...]}`, best first. Search covers only the newest version of each object and skips objects whose newest version is a delete.

| Hit field | |
|---|---|
| `source`, `kind`, `object_id`, `version`, `occurred_at` | The `source_records` row |
| `title` | PR or issue title (Jira: `KEY summary`), first line of a commit, or the start of a message |
| `snippet` | `ts_headline` with the matches in `**bold**` |
| `url` | GitHub `html_url`, the Jira browse URL built from the site and key, the GitLab `url`, or a Slack permalink if stored. Otherwise null |
| `score` | `ts_rank` |

### `get_record`

| Argument | Type | |
|---|---|---|
| `source` | string | A hit's `source` |
| `object_id` | string | A hit's `object_id` |

Returns the newest version of that object: `source`, `tenant`, `kind`, `object_id`, `version`, `occurred_at` and the full `record` JSON. A record over 50,000 characters comes back as truncated JSON text with `"truncated": true`. An unknown object is a tool error.

## Tests

[`test_mcp.py`](test_mcp.py) runs against a real Postgres built from the repo migrations, as the enrich tests do. It is the `MCP server against Postgres` step in CI.

```sh
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 apps/mcp/test_mcp.py
```

## Conventions

- This package is the only place MCP protocol handling lives.
- Graph access goes through shared code in `packages/` once a second consumer exists. Until then it may live here.

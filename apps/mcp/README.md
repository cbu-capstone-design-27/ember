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

**Identifiers first.** Before ranking, `search` looks for identifiers in the question:
- a Jira key (`PHO-87`) whose project is one of the user's Jira graphs
- a GitHub `owner/repo#n` for one of their repos
- a bare `#n` when they have exactly one GitHub repo

Each one is resolved straight to its node and record, which comes first with score 1. The records whose nodes `REFERENCES` it come right after, with score 0.5: Slack messages, comments, and pull requests naming it. BM25 alone matched `pho` on every ticket and `87` on PR #87.

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

## Local stack

One path runs Ember on this Mac, on the compose Postgres (pgvector, PG 18.6), never the cluster's. From the repo root:

```sh
cp .env.example .env    # first time; fill in what the checklist below asks for
scripts/local-stack.sh
```

It starts Postgres, applies the migrations and creates `ember_mcp` (`postgres-mcp-role`). Then it runs, until Ctrl+C, with logs in `/tmp/ember-local-stack`:

- the web app on that Postgres at http://localhost:3000, so the MCP sees the accounts and organizations you make there
- each source's enrich worker whose credentials are in `.env`; it prints which ones it skipped and why
- extract, then load with `GRAPH_STORE=postgres`
- embed, when `EMBEDDING_URL` is set. Without it, search runs on BM25 and graph traversal

It ignores `DATABASE_URL` from `.env`. Then sign up at http://localhost:3000 and create an organization. Its slug is your tenant. Register the MCP as in [Add it to an agent](#add-it-to-an-agent), with your email.

### Connecting accounts: what you still do by hand

No source pulls its history into `source_records` yet. Reconcile isn't built (ADR 0010, "Replay and backfill on connect"), and the pull scripts only write JSONL. Webhooks bring new events only, and they need a public HTTPS URL to intake (a tunnel to `localhost:8080`). For Jira and Slack, `seed_photon.py` imports a pull's JSONL under your tenant, since the photon fixtures are that same output. The running extract and load then build the graph.

- [ ] **Every source.** In `.env`: `SOURCE_CREDENTIALS_API_KEYS` and `SOURCE_CREDENTIALS_WEBHOOK_KEYS`, each `k1:<openssl rand -base64 32>` (`packages/source-credentials`).
- [ ] **Jira.** Create an OAuth 2.0 (3LO) app in the Atlassian developer console with scopes `read:jira-work`, `read:jira-user` and `manage:jira-webhook`, callback `http://localhost:3000/connect/jira/callback`, and sharing on. Set `JIRA_OAUTH_CLIENT_ID`, `JIRA_OAUTH_CLIENT_SECRET` and `JIRA_WEBHOOK_URL` (it must be HTTPS, or Connect Jira is disabled). Connect in Settings → Connections. History, with an API token for `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN`:
  ```sh
  .venv/bin/python services/jira-ingestion/pull_test.py --project KEY --output /tmp/jira.jsonl
  DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember \
    .venv/bin/python apps/mcp/seed_photon.py --no-embed --tenant <your org slug> --jira /tmp/jira.jsonl
  ```
- [ ] **Slack.** There is no web connect flow yet (EMBER-148). Create the app from `services/slack-ingestion/slack-app-manifest.json`, install it and set `SLACK_BOT_TOKEN`. History: `pull_test.py --output /tmp/slack.jsonl`, then `seed_photon.py ... --slack /tmp/slack.jsonl` as above. For live events, map the workspace to your tenant with `infra/ansible/playbooks/map-tenant.sql` against the local database (`make map-tenant` only targets the cluster):
  ```sh
  psql postgresql://ember:change-me-locally@localhost:5432/ember \
    -v source=slack -v account=<team id> -v tenant=<your org slug> -f infra/ansible/playbooks/map-tenant.sql
  ```
- [ ] **GitHub.** Register a dev GitHub App with callback `http://localhost:3000/connect/github/callback`, "Request user authorization (OAuth) during installation" on, and read access to Metadata, Contents, Issues, Pull requests and Members. Generate a client secret and a private key. Set `GITHUB_APP_ID`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_SLUG` and `GITHUB_APP_PRIVATE_KEY_PATH`, then connect in Settings → Connections. History: none yet. `pull_test.py`'s output isn't in a shape `seed_photon.py` reads, so only webhook events arrive, and those need `GITHUB_WEBHOOK_SECRET` and a public URL to intake.
- [ ] **GitLab.** Connect in Settings → Connections by pasting a `read_api` token for an HTTPS instance (`GITLAB_WEBHOOK_URL` must be HTTPS too). History: none in the graph yet. `pull.py` writes JSONL only, and extract doesn't map GitLab.

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
| `score` | 1 for an identifier the question names, 0.5 for a record referencing it, else the fused score: the sum of `1 / (60 + rank)` over the lists that found it |
| `related` | Up to five current facts around the hit's node: `type`, `fact` ("Eli is assigned to Rotate the signing key"), `valid_at` |

### `get_record`

| Argument | Type | |
|---|---|---|
| `source` | string | A hit's `source` |
| `object_id` | string | A hit's `object_id`. For Jira, the key (`PHO-87`) works too and maps to the issue id |
| `raw` | boolean | Default false. True returns the whole stored payload as `record` |

Returns the newest version of that object: `source`, `tenant`, `kind`, `object_id`, `version`, `occurred_at`, `title` and `url`.

By default it adds a compact record, one per source, with fields left out when they're missing:
- `key`, `status`, `assignee`, `reporter`, `created`, `updated` and `labels`
- `description` as plain text: Jira's rich text (ADF) is flattened, and it's cut at 4,000 characters
- `comment_count`: a Jira issue's stored comments, GitHub's `comments`, or a Slack message's replies

With `raw: true` it returns the full `record` JSON instead. A record over 50,000 characters comes back as truncated JSON text with `"truncated": true`.

Both forms also have:
- `node` (`label`, `key`, `name`, `summary`), read through Graphiti's `EntityNode`
- `facts`, the current 1-hop facts, through `EntityEdge.get_by_node_uuid`
- `graph` (`name`, `scope`, `source`)

`node` and `graph` are null for a record that isn't loaded yet. An object the user can't see is a tool error.

### `history`

Takes `source` and `object_id` (a Jira key works too). Returns every fact about the object's node, ended ones included, oldest first: `type`, `fact`, `valid_at`, `invalid_at` (null while it holds), `set_by` and `ended_by`. Each of those is the episode (`episode` key) and source record version (`source`, `object_id`, `version`) behind it, or null. Graphiti's `EntityEdge` has no field for the episode that ended a fact, so `ended_by` is read from `graph.edges.invalidated_by` in the same reader transaction.

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

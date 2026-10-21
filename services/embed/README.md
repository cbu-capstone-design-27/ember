# embed

EMBER-77. Embeds every `source_records` row with the team's embedding model ([ADR 0001](../../docs/adr/0001-embedding-model.md), [`services/embedding`](../embedding/README.md)) and stores the vector in `source_records.embedding` (pgvector, `vector(1024)`, HNSW cosine index). One row is one chunk; rows are not split. EMBER-78 searches these vectors.

## What it does

- Claims up to `EMBED_BATCH` (32) rows with `embedded_at is null`, `for update skip locked`, oldest `id` first.
- Builds `embed_text` per row from the record: GitHub title, body and comment bodies, commit messages with changed paths, and PR files; Jira `KEY: summary` with the description or comment (ADF flattened to text); GitLab title, description, notes and commits; Slack message text. Anything else is the record as compact JSON. Documents get no prefix (ADR 0001).
- Cuts `embed_text` to 8000 characters. The Spark serves `--max-model-len 8192` tokens, not the model's 32k, and 8000 characters stays under that even at one token per character.
- Sends the batch in one `POST {EMBEDDING_URL}/v1/embeddings`, then writes `embedding`, `embed_text` and `embedded_at = clock_timestamp()` in the claim's transaction. Each row has its own savepoint, so a row Postgres rejects fails alone. If the service rejects the batch with a 4xx, rows are sent one at a time so only the bad one fails. Failed rows are skipped until the next 30 s poll.
- Retries 429, 5xx and network errors up to 8 times, backing off 1, 2, 4 … 60 s, or `Retry-After` on a 429.
- Never re-embeds a row that has `embedded_at`.
- Wakes on `LISTEN source_records` (an insert trigger, EMBER-77 migration), catches up on start, and polls every 30 s. One batch is in flight at a time, since the shared GPU endpoint is the limit.
- On start, embeds one probe text and refuses to run (exit 2) unless the vector has 1024 dimensions.
- SIGINT or SIGTERM: stops claiming, finishes the batch in flight, exits 0.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r services/embed/requirements.txt
DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable \
EMBEDDING_URL=http://<spark>:8000 EMBEDDING_API_KEY=... \
  .venv/bin/python3 services/embed/embed.py
```

| Variable | Default | |
| --- | --- | --- |
| `DATABASE_URL` | required | Secret `postgres-app`, key `uri`, on the cluster |
| `EMBEDDING_URL` | required | Base URL, without `/v1` |
| `EMBEDDING_API_KEY` | required | The service's key |
| `EMBEDDING_MODEL` | `qwen3-embedding-0.6b` | The served name |
| `EMBED_BATCH` | 32 | Rows per request |
| `SOURCE_RECORDS_CHANNEL` | `source_records` | NOTIFY channel |

Postgres needs the `vector` extension. Locally, the compose `postgres` profile runs `pgvector/pgvector`; on the cluster, the CloudNativePG image ships it.

## Tests

`test_embed.py` runs against a real Postgres with pgvector and a stub embedding server. CI runs it in the `sanity` job.

```bash
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 services/embed/test_embed.py
```

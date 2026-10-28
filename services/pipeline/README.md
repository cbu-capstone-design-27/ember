# services/pipeline

Ingestion pipeline and job queue: pulls activity from GitHub, Jira, and Slack, extracts entities and relationships, and writes them to the Neo4j graph.

## Status

The local loader (EMBER-45, below) reads connector output and loads it into Neo4j. The extract and load workers (EMBER-85, below) take `source_records` from Postgres into Neo4j. The job queue is not here yet; it lands under its own Jira ticket.

Design: `docs/ingestion/architecture.md` (EMBER-41, ADR 0004). This service becomes the enrich, extract and load workers and the scheduler described there.

## Conventions

- Long-running / background work lives here, not in `apps/`.
- Local infrastructure it depends on (Neo4j today) is defined in `infra/docker-compose.yml`.

## Loader (EMBER-45)

Loads connector JSONL into Neo4j, one subgraph per source (`<tenant>_github`, `<tenant>_jira`,
`<tenant>_slack`), then checks the graph against the schema in `docs/graph-schema.md` and lists
every mismatch.

Put the connector output under `data/raw/<tenant>/`. Files can sit anywhere under that folder
(any layout; every `*.jsonl` is read). The source of each record is read from its envelope's
`type`, not from the folder or file name, so a GitHub file in a folder called `misc/` is still
loaded as GitHub. Then run:

    docker compose -f infra/docker-compose.yml --env-file .env --profile pipeline run --rm pipeline

Without Docker, from `services/pipeline`:

    python -m loader --tenant brandon-dev --input ../../data/raw/brandon-dev
    python -m loader --tenant brandon-dev --input ../../data/raw/brandon-dev --dry-run

`--dry-run` maps and checks everything in memory and needs no database. `--reset` deletes the
tenant's own subgraphs first; it never touches other tenants.

Arguments after the service name replace the compose `command:` entirely, so to pass `--reset`
under Docker give the full list:

    docker compose -f infra/docker-compose.yml --env-file .env --profile pipeline run --rm pipeline \
      --tenant brandon-dev --input /data/raw/brandon-dev --rejects /data/out/rejects.jsonl --reset

With the default `PIPELINE_NEO4J_URI` (`bolt://neo4j:7687`) the compose Neo4j must be up first:
`docker compose -f infra/docker-compose.yml --env-file .env up -d neo4j`. To load into the shared
cluster instead, set `PIPELINE_NEO4J_URI` to its `bolt://` address and the cluster's
`PIPELINE_NEO4J_USER` / `PIPELINE_NEO4J_PASSWORD` in `.env`. The pipeline reads these instead of
`NEO4J_USER` / `NEO4J_PASSWORD`, which keep configuring the local compose Neo4j. To load another
data folder (for example Ryan's synthetic data), mount it under `data/` and set
`PIPELINE_INPUT=/data/raw/<folder>` (a path inside the container); the default is
`/data/raw/<EMBER_TENANT>`. On Linux hosts
`data/out` must be writable by the container user (`nobody`) for `rejects.jsonl`:
`mkdir -p data/out && chmod a+rwX data/out`.

A rerun without `--reset` only adds or updates data: it never removes properties or edges. When two
records describe the same node and disagree, the later line in the input wins, except `embed_text`,
where the longer value wins. Use `--reset` to start the tenant from scratch.

The loader exits 2 without touching the database if the input folder is missing or holds no
`*.jsonl` (files inside `*.parts` folders are ignored), and refuses `--reset` when no record in the
input maps to a node, so a wrong path can never wipe a tenant.

Records the loader cannot map go to `rejects.jsonl` (`data/out/rejects.jsonl` under compose) with
a reason. The file is removed at the start of every run, so it only ever describes the latest run.
Records of a kind it does not map yet (for example Jira worklogs) are counted as skipped. The exit
code is 1 if anything was rejected or the schema check found a mismatch, so a clean run is exit code 0.

Tests: `cd services/pipeline && python -m unittest discover -s loader -t . -p "test_*.py" -v`.
The Neo4j integration test runs only when `NEO4J_TEST_URI` is set.

## Extract and load workers (EMBER-85)

The stages after enrich (`docs/ingestion/architecture.md`), on ADR 0002's fallback path (deterministic
mapping with EMBER-45's mappers, no LLM, no Graphiti):

- `python -m loader extract` reads `source_records`, maps each row to one `episodes` row (`attrs` holds
  the nodes and edges, `text` a short summary) and never touches Neo4j.
- `python -m loader load` writes unloaded episodes to Neo4j, one subgraph (`<tenant>_<source>`) at a time
  under a Postgres advisory lock, up to `LOAD_BATCH` (100) per job in the source's order, and stamps
  `episodes.loaded_at` in the same transaction.

Both read `DATABASE_URL` from the environment (not `.env`); load also reads `NEO4J_URI` (default
`bolt://localhost:7687`), `NEO4J_USER` (default `neo4j`), `NEO4J_PASSWORD` and optionally `NEO4J_DATABASE`.
`CONCURRENCY` (default 4) sets how many jobs run at once. Each wakes within a second on `NOTIFY`
(`source_records` / `episodes`), catches up at start, polls every 30 s, and on Ctrl+C or `SIGTERM` finishes
the jobs in flight and exits. `--once` drains what is waiting and exits.

Rules worth knowing:

- **Scope.** Jira issues and comments, and Slack messages, users and channels. GitHub, GitLab, Teams and
  other kinds (Jira worklogs, Slack memberships) are not extracted yet; extract logs how many are waiting
  when it starts.
- **Order.** Every node and edge stores the source's `occurred_at`. A version older than the stored one
  only fills properties the node lacks, so a late, older edit never undoes a newer one.
- **Deletes.** A deleted Slack message or Jira issue or comment sets `deleted: true` on its node, by the
  delete's time. The node is kept and a late edit cannot clear the flag.
- **Edges to nodes not loaded yet** (a comment whose issue comes in a later batch) create a bare
  `placeholder: true` node on the target's key, which the target's own episode fills in.
- **Reruns** change nothing: extract skips records that already have an episode from this
  `EXTRACTOR_VERSION` (`loader/extract.py`; bump it to re-extract everything), and load only takes
  episodes with `loaded_at` null.
- **Failures.** Postgres or Neo4j unreachable: retried with backoff, episodes stay unloaded. A record the
  mappers reject: logged and counted, left alone until the worker restarts (no error table yet; the job
  queue's dead letter replaces this).

Try it locally with the photon demo data (made-up), from the repo root. Start and migrate the compose
Postgres as in `apps/mcp/README.md`, start the compose Neo4j, then:

    docker compose -f infra/docker-compose.yml --env-file .env up -d neo4j
    export DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable'
    python3 apps/mcp/seed_photon.py --no-embed     # photon Jira and Slack rows into source_records, tenant photon
    cd services/pipeline
    python -m loader extract --once
    NEO4J_PASSWORD=change-me-locally python -m loader load --once

The graph then holds subgraphs `photon_jira` and `photon_slack`. Leave off `--once` to keep the workers
running and waiting for new rows.

Tests: `test_extract.py` runs with the unit tests. `test_pg_load.py` needs a Postgres with pgvector and
dbmate, and is CI's `Pipeline loader against Postgres` step:

    PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable'         python -m unittest loader.test_pg_load -v

With `NEO4J_TEST_URI` and `NEO4J_PASSWORD` set it also loads into that Neo4j (tenant `it-pg-load`, reset
by the test).

## Models (EMBER-49)

The pipeline calls a chat model and an embedding model chosen by a profile in `config/models.toml`.
The default profile is `local-spark`: Qwen3-30B-A3B for chat and Qwen3-Embedding-0.6B for embeddings, both
on the DGX Spark (why: `docs/adr/0008-local-llm-on-the-spark.md`). `openai-dev` calls hosted OpenAI models
(`docs/adr/0007-cloud-llm-and-embedding.md`) and drops in once credits arrive. To switch, set
`MODEL_PROFILE` (or pass `--profile`) or edit that file. No code changes. Empty variables are treated as
unset, so the blank `LLM_BASE_URL=` / `EMBEDDING_BASE_URL=` lines in `.env` leave the profile's URLs alone.

Keys are read from environment variables, never from the repository. Copy `.env.example` to `.env`. For
`local-spark` set `LLM_API_KEY` and `EMBEDDING_API_KEY` (generated on the Spark, in its `~/ember/.env`),
and `EMBEDDING_BASE_URL` (the Spark's public address, port 8000). The chat model is private to the Spark:
tunnel its port with `ssh -L 8001:127.0.0.1:8001 shared-dev` and the profile's default
`http://localhost:8001/v1` works. On a cluster node, run the same tunnel under a supervisor (systemd or
autossh) so it restarts if it drops (ADR 0008). For `openai-dev` set `OPENAI_API_KEY` and a monthly
spending limit on the provider account.

Deploy or refresh the servers on the Spark with `infra/spark/deploy-llm.sh` (chat) and
`infra/spark/deploy.sh` (embeddings).

Check that the models answer (one chat call and one embedding call). The first
command runs from `services/pipeline`; the docker command runs from the repo root:

    cd services/pipeline && python -m models smoke
    docker compose -f infra/docker-compose.yml --env-file .env --profile pipeline run --rm \
      --entrypoint python pipeline -m models smoke

Embed during a load:

    python -m loader --tenant brandon-dev --input ../../data/raw/brandon-dev --embed

`--dry-run --embed` still calls the embedding API (only Neo4j is skipped). `--profile` only matters
with `--embed`; without it the loader exits with a usage error.

Under compose the arguments after the service name replace `command:` entirely, so enabling `--embed`
means giving the full list:

    docker compose -f infra/docker-compose.yml --env-file .env --profile pipeline run --rm pipeline \
      --tenant <tenant> --input /data/raw/<tenant> --rejects /data/out/rejects.jsonl --embed

Each node's `embed_text` is embedded unprefixed, the vector is stored on the node, and the model is
recorded once in the `EmberConfig` node. The first `--embed` run records the embedding model in the
graph, and a different model is refused afterwards: a graph holds vectors from one model only
(ADR 0001), so switching means re-embedding everything. Delete the
`(:EmberConfig {key: 'embedding'})` node and each tenant's `embedding` and `embedding_text_hash`
properties deliberately, then rerun with `--embed`.

With `openai-dev`, embedding a small input costs a few cents; set a spending limit on the provider account
before the first real run. `local-spark` costs nothing per call.

Tests (no key, no network): `cd services/pipeline && python -m unittest discover -s models -t . -p "test_*.py" -v`.

## Benchmarks (EMBER-58)

`python -m benchmarks run --tenant <tenant> --input <raw folder>` measures loading correctness and
retrieval quality (BM25, vector, hybrid). The retrieval suite makes paid embedding calls and needs
`OPENAI_API_KEY`; `--suite loading` makes none. What each suite measures, how to write the questions,
how to read the scores, the exit codes, and what is not measured yet: `docs/benchmarks.md`. Saved
results: `benchmarks/results/`. The baseline scores are not recorded yet.

Tests (no key, no network): `cd services/pipeline && python -m unittest discover -s benchmarks -t . -p "test_*.py" -v`.

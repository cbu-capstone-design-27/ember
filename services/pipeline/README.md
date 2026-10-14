# services/pipeline

Ingestion pipeline and job queue: pulls activity from GitHub, Jira, and Slack, extracts entities and relationships, and writes them to the Neo4j graph.

## Status

The local loader (EMBER-45, below) reads connector output and loads it into Neo4j. Live ingestion, extraction and the job queue are not here yet; each lands under its own Jira ticket.

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

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
`docker compose -f infra/docker-compose.yml --env-file .env up -d neo4j`. On Linux hosts
`data/out` must be writable by the container user (`nobody`) for `rejects.jsonl`:
`mkdir -p data/out && chmod a+rwX data/out`.

Records the loader cannot map go to `rejects.jsonl` (`data/out/rejects.jsonl` under compose) with
a reason. The file is removed at the start of every run, so it only ever describes the latest run.
Records of a kind it does not map yet (for example Jira worklogs) are counted as skipped. The exit
code is 1 if anything was rejected or the schema check found a mismatch, so a clean run is exit code 0.

Tests: `cd services/pipeline && python -m unittest discover -s loader -t . -p "test_*.py" -v`.
The Neo4j integration test runs only when `NEO4J_TEST_URI` is set.

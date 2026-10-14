# Agent instructions for Ember

Read this before writing code here. Every agent builds the ingestion listener and workers the same way. Jira: EMBER-73.

## Ingestion architecture

The design is [`docs/ingestion/architecture.md`](docs/ingestion/architecture.md) ([ADR 0004](docs/adr/0004-ingestion-architecture.md)). In short:

- **intake** is the one listener for every source, at `POST /webhook/{source}`. It verifies the delivery, stores it as it arrived in `raw_events`, and answers 200. It never calls a source API.
- **enrich**, one worker per source, reads `raw_events`, fetches only what the payload lacks, and writes `source_records`. extract and load follow the same pattern further down.
- **Postgres** holds every hand-off: `raw_events`, `source_records`, `episodes`, `cursors`, `installations`, `rate_limits` ([ADR 0005](docs/adr/0005-postgres-on-the-cluster.md)).
- **`unique (source, delivery_id)`** on `raw_events` makes a re-sent delivery a no-op. Each adapter derives `delivery_id` so it is stable across the source's retries.
- **Source ordering value (EMBER-63).** Arrival order is not the source's order: retries, overlapping deliveries and reconcile rows arrive late. Workers order and apply changes by the source's own ordering value, and a change applies only if it is newer than what is stored for that object. EMBER-63 defines the column's name and type in `architecture.md`. Until then, do not invent one. [`slack-webhooks.md`](docs/ingestion/slack-webhooks.md#ordering-value-ember-63) shows how one source derives the value.

**Schema source of truth:** the SQL block in [`architecture.md`, Storage](docs/ingestion/architecture.md#storage). Do not copy it elsewhere. The cluster gets it from the dbmate migrations in [`infra/k8s/apps/postgres/migrations`](infra/k8s/apps/postgres/migrations), and CI fails if the two differ. A schema change edits both, as a new migration file. Never edit a merged migration.

Per-source behavior (event types, `delivery_id`, ordering, gaps) lives in `docs/ingestion/<source>*.md`.

## Worker rules

Every intake and stage worker must:

- [ ] Define SQLAlchemy models for the tables it touches, matching the schema in `architecture.md`. Models map tables only. dbmate migrations own the schema, so never call `create_all` or generate migrations from models.
- [ ] Connect through `DATABASE_URL` and nothing else. On the cluster it comes from Secret `postgres-app` (`uri` key).
- [ ] Run against a local Postgres in Docker for development and tests, on the version the cluster runs. CI's `Postgres migrations match the ingestion doc` step in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) shows the container and the dbmate setup.
- [ ] Use asyncio, and wake on Postgres `LISTEN/NOTIFY` within 1 s of the commit that created its work. Mechanism, including the 30 s poll fallback: [Workers: start, wake and stop](docs/ingestion/architecture.md#workers-start-wake-and-stop).
- [ ] Handle several rows concurrently in one process, bounded by `CONCURRENCY` ([Concurrency](docs/ingestion/architecture.md#concurrency)).
- [ ] On start, catch up on every row inserted while it was stopped. Work waits in Postgres, so a stopped worker loses nothing.
- [ ] Shut down gracefully on Ctrl+C (`SIGINT`) and `SIGTERM`: stop taking new work, finish the job in progress, then exit.
- [ ] Be idempotent on `(source, delivery_id)`. Processing the same delivery twice changes nothing: intake relies on the unique key, and later stages write on their own natural keys (for enrich, `source_records`' `(source, tenant, object_id, version)`).
- [ ] Apply changes by the source ordering value, never by arrival order.
- [ ] Classify failures as throttled, transient or permanent, as in [Failures and replay](docs/ingestion/architecture.md#failures-and-replay).

## Repo conventions

- **Branches:** `type/EMBER-<n>-<slug>`, from `develop`. Use `feature/EMBER-<n>-<slug>`, or `hotfix/EMBER-<n>-<slug>` from `main`. See [`CONTRIBUTING.md`](CONTRIBUTING.md).
- **Jira key everywhere:** PR titles start with it (`EMBER-<n>: ...`), and it appears in the PR body (the [template](.github/PULL_REQUEST_TEMPLATE.md) requires it) and in commit messages.
- **PRs target `develop`.** `main` only receives releases and hotfixes.
- **Meaningful CI only.** Each service's `test_*.py` runs as its own step in the `sanity` job of `ci.yml`. Enroll tests that check real behavior. Do not add placeholder or smoke-only jobs.

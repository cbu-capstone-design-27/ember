# ADR 0005: Postgres on the cluster with CloudNativePG

- Status: Accepted
- Date: 2026-10-13
- Jira: EMBER-61

## Context

ADR 0004 keeps every ingestion stage's state in one Postgres on the cluster: `raw_events`, `source_records`, `episodes`, cursors, installations, rate limits, and the job queue. It left open how Postgres is installed. EMBER-61 asks for it to be deployed by Flux, keep its data across restarts, be reachable in-cluster and over Tailscale, keep credentials out of the repo, give services `DATABASE_URL`, and apply schema changes by a repeatable step rather than by hand.

The workers are two Hyper-V VMs with about 9.5 GiB each. Neo4j takes 2 Gi on hv-worker-1.

We looked at three ways to run it:

- **Bitnami's `postgresql` chart.** Bitnami stopped publishing free, versioned images in 2025, so the chart would run images nobody patches.
- **A plain StatefulSet** on the official `postgres` image. No operator, but the password, the `DATABASE_URL` Secret, roles and backups would all be ours to write.
- **The CloudNativePG operator**, a CNCF project that runs Postgres from a `Cluster` object.

## Decision

Run Postgres as a one-instance CloudNativePG `Cluster`, and apply the schema with dbmate.

- **Credentials.** The operator generates the owner role's password and keeps it in Secret `postgres-app`, whose `uri` key is a complete connection string. Services read it as `DATABASE_URL`. Nothing about the password is in Git, not even SOPS-encrypted.
- **Later needs are fields, not rebuilds.** ADR 0004 wants a database role per component, which `spec.managed.roles` declares. Replicas are `instances`, and backups to object storage are a plugin on the same object.
- **Fits what is there.** The operator is one small pod, installed by Flux from `infrastructure/cnpg` so its CRDs exist before `apps/`. The instance follows Neo4j's pattern: a `local-path-retain` volume, pinned to a node (hv-worker-2, so the two databases are on different workers), and published to the tailnet through ServiceLB.

**Migrations: dbmate**, run by Job `postgres-migrate`. Migrations are plain SQL files with up and down sections. dbmate records what it applied in `schema_migrations`, runs only what is new, and reads `DATABASE_URL` itself. Flux recreates the Job when a migration is added.

- The operator's own `postInitApplicationSQL` runs once, at bootstrap, so it cannot apply later changes.
- Alembic would tie migrations to a Python service image that does not exist yet and add SQLAlchemy. Plain SQL keeps the statements in `docs/ingestion/architecture.md` as they are, and CI checks the two build the same schema.

## Consequences

- One instance means no failover. While hv-worker-2 is down, Postgres is down and intake answers 503 (ADR 0004, Failures and replay).
- The data is only on hv-worker-2's disk until backups are configured. Rebuilding that node loses it.
- Every component connects as role `ember` until per-component roles are added.
- The Postgres image is pinned in `cluster.yaml`, so Postgres changes version only when someone edits it, not when the operator is upgraded.
- A schema change is a new migration file listed in `apps/postgres/kustomization.yaml`, plus the same change in the architecture doc. A merged migration is never edited, because the cluster will not run it again.
- The job queue library's own tables (Procrastinate, ADR 0004) go in as a migration when the queue lands.

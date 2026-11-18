# ADR 0012: One backend image on Postgres, with the auth API kept in TypeScript for now

- Status: Proposed
- Date: 2026-11-13
- Jira: [EMBER-160](https://ember-capstone.atlassian.net/browse/EMBER-160); the Connect flows move in with [EMBER-168](https://ember-capstone.atlassian.net/browse/EMBER-168)
- Supersedes, once accepted: [ADR 0004](0004-ingestion-architecture.md)'s Deployment section (one Deployment per stage), and [ADR 0006](0006-web-app-stack.md)'s "pages and the auth API in one app"

## Context

Ember runs today as eleven workloads, each with its own image or command, Deployment and Secret (`infra/k8s/apps`): nginx, intake (2 replicas), `enrich-{github,gitlab,jira,slack}`, embed, web, `llm-tunnel`, Postgres, and the Jira keep-alive CronJob. ADR 0004 plans more: extract, load, and a scheduler CronJob per source.

That layout is a lot to run anywhere except this cluster:

- **Docker.** A self-hosted install has to start every one of those services. `infra/docker-compose.yml` already lists 16 of them.
- **Cloud.** The plan is to run on one small VM (an e2 instance with Postgres and Ember on it), not a rented Kubernetes cluster. Running a dozen Python processes, each holding its own runtime in memory, is wasteful on one box. The same is true on the cluster's two 4 GB nodes.

The design already allows something simpler. The stages share nothing but Postgres (ADR 0004). Every worker pulls its work with `LISTEN/NOTIFY` and `FOR UPDATE SKIP LOCKED`, so any number of identical processes can take work from the same tables. Embedding and the LLM are remote HTTP endpoints ([ADR 0007](0007-cloud-llm-and-embedding.md), `services/embed`), so no worker needs a GPU or a model in memory.

The web app (`apps/web`) is more than pages:

- **Better Auth** runs sign-in, organizations, members, roles and invitations. An organization is a tenant (EMBER-124, [ADR 0010](0010-connecting-sources.md#tenants)).
- **The CLI already signs in through it**, with the device flow (RFC 8628) at `/api/auth/device/*`, then `/api/whoami` with a bearer token (`apps/cli/src/auth.rs`).
- **The Connect flows** for GitHub and Jira (EMBER-130, EMBER-131) and the sealing of `source_credentials` live in it.

The pages will be replaced by a different frontend and by the CLI, which is a thin client of Ember's API. The server half of the web app still has to live somewhere.

Postgres is already Ember's only database. Neo4j has been decommissioned, and the graph runs on Postgres through Ember's Graphiti fork ([ADR 0011](0011-graph-in-postgres.md)). This ADR packages that as it is and changes nothing about the graph.

## Decision

### Deployables

Ember ships as two stateless images on one Postgres, behind a TLS proxy:

| Piece | Language | What it holds | Scales by |
|---|---|---|---|
| **`ember-backend`** | Python | Intake (`POST /webhook/{source}`), the query API (search, graph questions, source status), every stage worker (enrich per source, extract, load, embed), and the scheduler (reconcile, Jira keep-alive) | Replicas, all taking work from the same Postgres |
| **`ember-api`** | TypeScript | Today's `apps/web` without its pages: Better Auth (users, organizations, roles, invitations, the CLI device flow, bearer tokens), the Connect flows and their OAuth callbacks, and the sealing of `source_credentials` | Replicas |
| **Postgres** | n/a | Everything stateful: ingestion tables, the queue, pgvector, the graph (through the Graphiti fork), and Better Auth's `web` schema | One instance |
| **TLS proxy** | n/a | Public HTTPS for webhooks and OAuth callbacks. One origin for clients: `/api/auth/*` and `/connect/*` go to `ember-api`, everything else to `ember-backend` | n/a |

Clients (the CLI, MCP, and any future frontend) use only the public HTTP API. None of them reads Postgres.

### One image, one process, components chosen at start

`ember-backend` (`services/backend`) is one image with one entry point:

```
python -m ember run                          # every configured component in one process
EMBER_COMPONENTS=intake python -m ember run  # HTTP only
EMBER_COMPONENTS='enrich:*,job:*' python -m ember run   # stage workers and scheduled jobs only
python -m ember check                        # what run would start, and why the rest is disabled
python -m ember migrate                      # planned: dbmate, then exit
```

- **Docker and small installs** run everything.
- **Kubernetes** can run the same image as one Deployment, or split it with `EMBER_COMPONENTS` when intake must keep answering under heavy worker load.

Inside the process the stages are built from a few shared classes, not copied per source ([`services/backend/README.md`](../../services/backend/README.md)):

- **Component:** anything long-running: intake, an enrich worker, a scheduled job. `start()`, `run(stopping)`, `close()`.
- **Connector:** one per source. It loads that source's settings and builds its parts: the webhook adapter, the enrich worker, its jobs, and later its Connect flow (EMBER-168). Each part is built on its own, so a missing API token doesn't stop a source's webhooks.
- **QueueWorker:** the claim loop every stage worker shares: `LISTEN/NOTIFY`, catch-up, `FOR UPDATE SKIP LOCKED` in the source's order, `CONCURRENCY`, failure classes.
- **Controller and supervisor:** the controller builds everything from settings and handles signals. The supervisor runs each component in its own task and restarts one that crashes, with backoff, without touching the others. A part whose settings are missing or unusable is disabled with the reason, shown on `GET /status`; only a missing `DATABASE_URL` stops the process.
- **Dependencies** are passed to constructors from one place (the controller), with no DI framework. Each component makes its own Postgres pool, so one can't starve another.

The worker rules in `AGENTS.md` still apply to each component inside the process: asyncio, wake within 1 s on `NOTIFY`, `CONCURRENCY` per stage, catch-up on start, graceful `SIGTERM`, idempotent writes, ordering by `occurred_at`.

### Webhooks are never blocked by worker jobs

Intake must answer Slack and Teams within 3 s. Run together, intake shares a process with the workers, so:

- network-bound work (source APIs, the LLM, embedding, Postgres) stays on the event loop;
- CPU-bound work (Graphiti extraction steps, large diffs) runs in a process pool, never on the event loop.

If intake's latency still suffers, split the components. No code changes. The process reports event-loop lag (`ember_event_loop_lag_seconds`) so a component that blocks the loop shows up.

### "Only one" is enforced in Postgres, never in memory

Every replica is identical, so anything that must run once is coordinated in the database:

| What | Today | With replicas of one image |
|---|---|---|
| Reconcile scheduling, Jira keep-alive | A CronJob each | In-process scheduler. Only the replica holding a Postgres advisory lock fires. The queue library's periodic tasks also dedupe |
| Load keeps one subgraph in `occurred_at` order | One replica, plus a lock per subgraph | An advisory lock per `{tenant}_{source}`, which ADR 0004's job queue table already requires |
| Migrations | Job `postgres-migrate` | `ember-backend migrate`, run once before rollout (a Compose one-shot service, or the existing Job). Never at every replica's start |

### Auth across the two images

- `ember-api` issues every token. The CLI's sign-in does not change.
- `ember-backend` accepts the same bearer token. It resolves the token to a user and that user's tenants by calling `ember-api`'s `/api/whoami`, extended to return the user's organizations and their tenants. The result is cached for 30 s, so a revoked token works for at most 30 s more.
- Only `ember-api` reads Better Auth's tables. Their layout is Better Auth's to change.
- Every query is scoped to the caller's tenants. MCP becomes a client of the query API instead of opening its own read-only connection (`apps/mcp`), so it gets the same scoping.

### Credential separation is traded away, on purpose

ADR 0004 keeps the webhook credentials (intake) and the API credentials (enrich) in different pods, and ADR 0010 seals them with separate key rings. In one process, both rings are in memory together.

At this scale that is accepted. The key rings stay separate in storage and in code, so an install that needs the split back runs `EMBER_COMPONENTS=intake` and `EMBER_COMPONENTS='enrich:*,job:*'` as separate Deployments, each given only its own ring.

### Deploying it

| | Docker (one VM) | Kubernetes |
|---|---|---|
| Ember | `compose.yaml`: `ember-backend`, `ember-api`, `postgres`, `caddy`, and a `migrate` one-shot | One Deployment each for `ember-backend` (or one per component group) and `ember-api`, deployed by Flux ([ADR 0003](0003-gitops-controller.md)) |
| Postgres | The official image with pgvector, on a volume | CloudNativePG, as today ([ADR 0005](0005-postgres-on-the-cluster.md)) |
| Backups | Scheduled `pg_dump` or disk snapshots, which the install must set up | CloudNativePG |

On the cluster this replaces intake, the four enrich Deployments, embed and the keep-alive CronJob with one Deployment, and drops the planned extract, load and scheduler workloads. `llm-tunnel` stays while EMBER-49 needs it.

### Follow-up: port `ember-api` to Python

The end state is one backend: `ember-backend` plus Postgres. A later ticket moves `ember-api`'s endpoints into `ember-backend` one at a time, behind the same URLs, so clients don't notice:

- users, organizations, members, roles and invitations;
- the device flow and bearer tokens;
- the Connect flows and the sealing of credentials.

Existing passwords carry over: Better Auth hashes them with scrypt, which Python can verify. `ember-api` is removed when its last endpoint has moved. That ticket also decides how the user tables move out of Better Auth's `web` schema.

## Sequence

1. Build `services/backend`: the runtime and intake first (EMBER-160), then one connector per PR (GitHub, Jira with its keep-alive as a scheduled job, GitLab, Slack), then embed and `migrate`. Each old service is deleted once the image does its job.
2. Move scheduling and the load lock to advisory locks. Remove the CronJobs.
3. Add the query API, with tenant scoping through `ember-api`. Move MCP onto it.
4. Remove the pages from `apps/web`, keeping it as `ember-api`.
5. Add `compose.yaml` for a one-VM install. Replace the per-service manifests with the two Deployments.
6. Later: `ember-api` goes when the Python port is done.

## Consequences

- An install is two images, Postgres and a TLS proxy. One VM can run it.
- Fewer Python runtimes in memory: one per replica instead of one per stage and source.
- Stages are code boundaries, not deployment boundaries. A stage can no longer be scaled or rolled back alone. Splitting with `EMBER_COMPONENTS` gets part of that back.
- One bad release affects every stage at once, where before it affected one Deployment. A crash at runtime does not: the supervisor restarts only the component that crashed.
- Two languages remain on the server until the follow-up port.
- The credential split between intake and enrich is optional, not enforced (see above). So are the per-component Postgres roles ADR 0004 planned, unless components are split.
- A Docker install takes on backups and monitoring that CloudNativePG and the cluster's Grafana Cloud setup do on Kubernetes.

## Open questions

- Is a 30 s cache on token checks acceptable, or should `ember-backend` check every request?
- Does the replacement frontend talk to the same public API as the CLI, or does it need endpoints of its own?
- What size VM does a real tenant need? Measure on the cluster first, once the backend runs as one image.

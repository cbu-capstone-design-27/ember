# ADR 0004: Ingestion architecture

- Status: Proposed
- Date: 2026-10-06
- Jira: EMBER-41

## Context

Each source has its own receiver today (`services/{github,gitlab,jira,slack}-ingestion`). Each one verifies a webhook, wraps it in the EMBER-39 envelope and writes it to stdout. The backfills (`pull_test.py`) are separate scripts with their own rate limiting. Nothing queues, retries, stores raw payloads, or writes to the graph yet. `services/pipeline` and its job queue (EMBER-2) are stubs.

What the sources send:

- **Four send the data itself.** GitHub, GitLab, Jira and Slack webhooks carry the object as it stood when the event happened. They do leave things out: no diffs, PR file lists or commits past the first 20 in a push, and no watchers or votes from Jira.
- **Teams only signals.** A Graph change notification carries IDs, and the message has to be fetched.
- **Not every source retries.** GitHub never redelivers a failed delivery, and Slack retries only three times. Every source can deliver out of order.

The graph is temporal (ADR 0002): it has to know what was true before, not only what is true now. Raw payloads must be kept so data can be re-processed without re-fetching (ADR 0001). The cluster is small: two ~4 GB workers, with Neo4j already on one of them.

## Decision

Ingestion is one parent listener followed by three stages that meet only in Postgres. The design is in `docs/ingestion/architecture.md`, with an editable diagram in `docs/ingestion/architecture.excalidraw`.

- **One listener, intake**, with one adapter per source:
  - It verifies each delivery, stores it unchanged in `raw_events`, enqueues an `enrich` job in the same transaction, and answers 200.
  - It never calls a source API.
  - It is one Deployment instead of one receiver per source, because what differs between sources is only verification, de-duplication keys and handshakes.
- **The payload is the record; fetching only fills gaps.** Payloads are kept because they hold history and deletes that a later fetch cannot recover. Workers fetch what a payload lacks, and fetch everything for Teams.
- **Three separate stages:**

  | Stage | Turns | Into | Depends only on |
  |---|---|---|---|
  | enrich, one per source | a raw event | a complete source record | that source's API and credentials |
  | extract | a source record | source-agnostic episodes | the ontology |
  | load | episodes | Neo4j | Graphiti and Neo4j |

  Each stage writes its output and enqueues the next job in one transaction. Retries and replays stay within a stage.
- **Postgres on the cluster holds every hand-off and the queue.** The queue is a library on the same database (Procrastinate proposed), not hand-written.
- **Workers are always-on Deployments that pull.**
  - An idle worker waits on `LISTEN` and wakes on the `NOTIFY` from the commit that enqueued its job.
  - On `SIGTERM` a worker finishes its current job; a job still running when the pod is killed is detected as stalled and runs again.
  - A scheduler CronJob per source enqueues `reconcile` jobs on that source's own interval, so missed deliveries are found.
  - Autoscaling with KEDA can come later without changing worker code.
- **Credentials are split.** Intake holds only webhook secrets. Each source's API credentials live only in that source's enrich worker. The LLM endpoint is the tenant's URL and API token, or our own cloud provider on the enterprise instance.
- **Retention.** Raw events are kept for 30 days after they are fully processed; one that never finishes stays until its source is removed. Deliveries from accounts with no tenant are stored unmapped and not processed.

## Consequences

- A fix to extraction re-runs extract over stored source records with no API calls. Replacing the graph engine (ADR 0002's fallback) replaces only load and re-runs it.
- Every source is received the same way. Adding a source means one adapter, one enricher and one extractor, with no new service, image or nginx route.
- At-least-once delivery everywhere. Each stage's output is unique on a natural key, so duplicate work changes nothing.
- One more stateful component (Postgres) on the cluster, which is also the single point everything depends on. Intake answers 503 while it is down, and reconcile recovers what non-retrying sources sent meanwhile.
- One extra hop per stage, which adds milliseconds next to API calls and LLM extraction.
- The receivers and backfills in `services/*-ingestion` are reorganized into `services/intake` and the pipeline workers, as `docs/ingestion/architecture.md` maps out.
- Open questions are listed at the end of the design doc: which stage calls the LLM (it follows ADR 0002's spike), the Postgres chart, whether the queue library can enqueue inside the caller's transaction, `source_records` and `episodes` retention, each reconcile schedule, and when to add KEDA.

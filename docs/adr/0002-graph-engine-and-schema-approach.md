# ADR 0002: Graph engine and schema approach

- Status: Proposed
- Date: 2026-09-29
- Jira: EMBER-34

## Context

Ember's graph has to answer "what is true now, what was true before, and why did it change" across GitHub, GitLab, Jira, Slack and Teams. Decisions get revised and work items change state, so facts need validity over time and old facts must stay queryable. The schema must work for five sources at launch and take more later without a rewrite.

We evaluated Graphiti (getzep/graphiti), a temporal knowledge graph engine that runs on Neo4j. As of 2026-09-29:

- Fact edges carry `valid_at`, `invalid_at`, `created_at` and `expired_at`. New facts that contradict old ones invalidate them instead of overwriting.
- Nodes and edges are partitioned by `group_id`.
- We supply the ontology as Pydantic models (entity types, edge types, and an edge-type map) passed to `add_episode()`. Graphiti creates the graph structure from that; we write no DDL for it.
- It embeds entity names and fact edges only, not source text chunks. There is no vector index on Neo4j; similarity is an exact scan.
- The embedding dimension is process-wide configuration. Graphiti stores no embedding model identity and supports no query instruction prefix.
- Entity types must be declared up front; only relationship names are free-form. Extraction quality depends on the LLM, and small local models are unreliable at structured output.

## Decision

Adopt Graphiti provisionally as the graph engine, keeping the ontology independent of it.

- The ontology lives in `services/pipeline/ontology/` as plain Pydantic models plus a registry mapping each source's payload kinds onto a small set of core types. Nothing in it imports Graphiti, so it can be reused with plain Neo4j if Graphiti is rejected. `docs/graph-schema.md` describes it.
- One subgraph per source per tenant, as in the architecture diagram: `group_id = <tenant>_<source>`. Phase 1 retrieval searches each subgraph. Every subgraph uses the same core types, so the phase 2 merge across sources joins on shared labels and keys. Cross-source links (a Jira key mentioned in Slack, one person across sources) are phase 2 work.
- Embedding identity and schema version are recorded in one `EmberConfig` node (ADR 0001), because Graphiti does not record them.
- Decisions are entities with a `SUPERSEDES` edge. Graphiti's edge invalidation records that facts changed; the `SUPERSEDES` edge records that a decision replaced another.

This ADR stays Proposed until a spike passes.

## Spike (required before Accepted)

Run the five fixtures in `packages/ingestion-envelope/fixtures/`, plus a small set of realistic Slack threads and GitHub PRs, through Graphiti with this ontology on the Spark-hosted LLM and embedding service. Accept only if:

1. Entities and edges land on the declared types without hand correction on most items.
2. Within one subgraph, a GitHub PR that closes a GitHub issue links to it, and a re-sent event does not create duplicates.
3. Retrieval over entity names and facts alone answers questions that today need message text. If it cannot, keep source text embedded separately (ADR 0001 already stores `embed_text`).
4. Ingest cost and latency per event are tolerable with the local model.
5. Custom attributes land as node properties, so the constraints in `ontology/schema.cypher` can be applied.

If the spike fails on 1 or 4, the fallback is deterministic structured mapping for the sources that already carry structure (GitHub, GitLab, Jira) and LLM extraction only for free text, on the same ontology.

## Consequences

- Temporal history comes from the engine, not from code we maintain.
- We depend on a fast-moving project, and on LLM quality for extraction. Moving to a hosted model later (Vertex AI or similar) is expected to help but is not assumed.
- Retrieval fit, Neo4j version support (the cluster runs Neo4j 2026.9.0; Graphiti documents 5.26) and ingest cost are unproven until the spike.
- Extending the schema is additive: a new source adds a registry entry, and a new core type adds a model and edge-map rows. Removing or renaming a type is a rebuild.

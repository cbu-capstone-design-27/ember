# ADR 0013: Postgres is the only graph store; Neo4j removed

- Status: Accepted
- Date: 2026-11-18
- Jira: EMBER-169
- Decided by Ryan Stoffel
- Supersedes: [ADR 0011](0011-graph-in-postgres.md)'s "keep Neo4j until the switch". The rest of ADR 0011 stands.

## Context

[ADR 0011](0011-graph-in-postgres.md) moved the graph to Postgres with a Graphiti fork, and kept Neo4j as the fallback until the Postgres version matched. That version now exists:

- The `graph.*` tables (EMBER-164) hold one graph per connected scope, and `services/pipeline/loader/pg_writer.py` writes them in the transaction that stamps `episodes.loaded_at`.
- The fork (`graphiti-core @ git+https://github.com/cbu-capstone-design-27/graphiti@ember-v0.31.2`) has a `PostgresDriver` over those tables (EMBER-158), and the MCP server reads the graph through it (EMBER-166).
- The embed worker fills `graph.nodes.embedding` and `graph.edges.fact_embedding`.

Neo4j only remained in the load step's default writer, the local tooling and the EMBER-146 reference. Keeping it meant two databases, two writers that had to agree, and a password in the repository's history.

## Decision

- Postgres is the only graph store. Ember reads and searches it with Graphiti on the fork's `PostgresDriver`.
- `python -m loader load` writes only to Postgres. `neo4j_writer.py`, `ontology/schema.cypher`, the `GRAPH_STORE` switch and the `neo4j` driver are removed.
- The EMBER-146 harness (`services/pipeline/graphiti_test/`) runs on the fork's `PostgresDriver`, in graphs of its own.
- Tools that only need to check the mapping (the JSONL loader, `e2e-check`, the benchmarks) run on the in-memory graph. Their Neo4j modes are removed.

## Consequences

- One database. Graph writes commit with `episodes.loaded_at`, and backups, access and row-level security cover the graph with everything else.
- The EMBER-146 reference now runs on Postgres. Comparing the fork with stock Graphiti on Neo4j isn't possible from this repo anymore. Two runs of the harness show two fork gaps: MMR search fails on nodes whose vector a resave nulled, and search and temporal results move between identical runs.
- `e2e-check` no longer runs Cypher sample queries. Its counts and schema check run in memory, and `test_pg_writer.py` checks what the graph tables hold.
- The cluster's Neo4j deployment, which lives in the private infra repo, and its password are retired as a follow-up for Jacob. The password has been in the repository's history since 2026-09-25, so it is changed, not only removed.

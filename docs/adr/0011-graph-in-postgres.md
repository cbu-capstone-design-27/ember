# ADR 0011: Move the graph to Postgres with a Graphiti fork, keeping Neo4j until it's ready

- Status: Accepted
- Date: 2026-11-04
- Jira: EMBER-118, EMBER-117
- Decided by the team, recorded by Ryan Stoffel
- Supersedes: [ADR 0002](0002-graph-engine-and-schema-approach.md)'s engine choice in the long term. Its ontology stays, and Neo4j stays in use until the switch below.

## Context

ADR 0002 chose Neo4j and Graphiti provisionally. The pipeline doesn't use Graphiti today: the loader writes Neo4j with plain Cypher (`neo4j_writer.py`), and on the cluster data stops at the embed worker. Graphiti runs only in the EMBER-146 harness (`services/pipeline/graphiti_test/`, [results](../research/graphiti-test.md)), and so far only with a fake model, so extraction quality, retrieval quality and real cost are still unknown.

The research is [`docs/research/postgres-graph.md`](../research/postgres-graph.md). It shows:

- **Store.** Plain tables answered every benchmark query in 0.8 to 2.8 ms at 100 photon-sized tenants. Neo4j 5.26 took up to 47.7 ms and AGE up to 524 ms. SQL/PGQ was reverted from PostgreSQL 19. Brandon notes that the cluster runs Neo4j 2026.9, which he reports has filtered vector search, so the vector gap may be smaller there. This was not re-tested.
- **Prototype.** It loaded 1,289 photon records into those tables and answered a temporal question for PHO-104: In Progress on 2026-09-25, Done on 2026-09-29, changed by Ryan Stoffel at 2026-09-28 17:17 UTC, with the last comment and pull request #231 linked. Those queries took under 6 ms, with no Graphiti. Status at every boundary of all 112 tickets matched the raw changelog.
- **What the prototype corrected.** Status history needs `HAS_STATUS` edges from a changelog mapper, because a ticket's episodes don't hold it. `valid_at` can't be a snapshot's record time.
- **What it didn't test.** Search, the fork and LLM extraction. The pull request link is evidence by URL and time, not a stated cause, and photon's changelog is generated from GitHub times.
- **Fork.** Graphiti v0.31.2 has no Postgres backend. A driver means about 100 interface methods plus 7 hooks where core still runs Cypher, on a storage layer upstream is still migrating.

## Decision

- **Long term,** the graph moves to Postgres, and Ember uses its own Graphiti fork on the Postgres graph.
- **For now,** Neo4j stays so work can continue. It is removed only when the Postgres version matches.

### Sequence

1. Test stock Graphiti on Neo4j (EMBER-146), and save the results as the reference. They inform how the fork is built.
2. Build the Postgres tables and load step (research Phase A), then the fork (Phases B and C). Skip what Ember doesn't use, such as communities and sagas.
3. Switch when the Postgres version passes Graphiti's integration tests and answers as well as or better than the reference. Neo4j is the fallback until then.

The research estimates 43 to 58 working days across its four phases, including the code graph.

## Open questions

The team decided only the direction above. The suggested answers are suggestions, not decisions.

1. **`SAME_AS` or one `Person` per tenant?** Suggested: add `SAME_AS`, because it keeps a wrong merge to one edge. Add `Status` and `HAS_STATUS` in the same ontology change, and settle EMBER-146's finding that Graphiti's default `RELATES_TO` collides with ours.
2. **How long are `episodes` kept?** Suggested: as long as the graph, because replays and older property values read them. Review when a tenant's table grows large.
3. **Which free text goes through `add_episode`?** Suggested: comments and human Slack messages over a length threshold, skipping bot feeds such as `#photon-github`. Set the threshold from EMBER-146's measured cost per message.
4. **Is Python plus TypeScript on the default branch enough for the first code graph?** Suggested: yes. Add open pull request branches when a question needs them.
5. **What do we offer upstream?** Suggested: offer the core hooks early, to reduce what the fork carries, and ask about the driver in an issue. Don't count on it being accepted, since earlier Postgres requests were closed without one.
6. **Who builds Phase A?** Not decided. Brandon's work (`neo4j_writer.py`, the load step) is what gets ported, and he asked that two people don't build it separately.

## What changes for current work

Nothing is retired yet.

- **`neo4j_writer.py` and the Neo4j deployment** stay until the switch.
- **EMBER-146** continues. Its results become the reference for the switch.
- **EMBER-119** keeps its fixed interface and runs on Neo4j and Graphiti first, so the backend can change later without changing the MCP server.
- **EMBER-122** is undecided. Ryan hasn't decided, and Brandon raised that it may not be needed.

## Consequences

- Ember runs two databases until the switch: Postgres for ingestion and pgvector, Neo4j for the graph. Graph writes and `episodes.loaded_at` stay separate commits until then.
- After the switch, Ember carries a fork of Graphiti and rebases it onto upstream releases.
- The research's corrections need an ontology change whichever way question 1 goes: a `Status` node and a `HAS_STATUS` edge.

### Prerequisites before real data moves

Brandon raised these for Jacob:

- **Postgres backups.** None are configured: `infra/k8s/apps/postgres/cluster.yaml` has no backup section.
- **The Neo4j password in the public repo.** It is in plain text in `infra/k8s/apps/neo4j/values.yaml`, marked dev-only, and in the repository's history since 2026-09-25. Moving it to a Secret isn't enough, because the old value stays in history, so it also has to be changed.
- **The `ember` Postgres password** needs changing. The operator generates it and it isn't in the repo, so this is a rotation, not a repo fix.

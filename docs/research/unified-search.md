# Research: unified semantic and graph search

Date: 2026-10-27. Jira: EMBER-84. Status: research, no decision. Recommendations here need an ADR before they bind anything.

Ember has two kinds of index over the same items. Semantic vectors find text that means the same thing as a question. The graph says how items relate: a pull request resolves an issue, a comment belongs to a ticket, a message replies to another. "Unified search" means using both in each direction:

- **Question → graph.** Given a plain-language question, return the right nodes, including ones whose text does not match the question but that are connected to items that do.
- **Node → related meaning.** Given a node (a ticket, a PR, a Slack thread), return the nodes related to it by meaning, structure or both, including across sources where no edge exists yet.

This document compares five families of methods on Ember's data and infrastructure, and proposes how each would be scored in the EMBER-58 benchmark.

## What Ember has today

| Piece | State | Where |
| --- | --- | --- |
| Ontology | 9 node types, 16 relationship types, one subgraph per source per tenant (`<tenant>_<source>`) | `docs/graph-schema.md`, `services/pipeline/ontology/` |
| Graph engine | Graphiti is only **Proposed** (ADR 0002, spike not run). The EMBER-45 loader writes Neo4j directly by structured mapping | `services/pipeline/loader/` |
| Edges actually written | Within one source only: `CONTAINS`, `AUTHORED`, `PART_OF`, `REPLIES_TO`, `RESOLVES` (GitHub PR closes issue), `REFERENCES` (Jira comment to its issue), `RELATES_TO`, `RESOLVES_TO`. No cross-source edges (phase 2) | `loader/mappers/` |
| Node vectors in Neo4j | `loader --embed` stores one vector per node from `embed_text`; no vector index, exact scan | ADR 0001, `graph-schema.md` |
| Record vectors in Postgres | `source_records.embedding`, pgvector `vector(1024)`, HNSW cosine, written by the embed worker | EMBER-77/89, `services/embed` |
| Vector search for agents | MCP `search` tool, cosine over `source_records`, no keyword fallback | EMBER-78, `apps/mcp` |
| Models | Qwen3-Embedding-0.6B (1024-d) and Qwen3-30B-A3B on the DGX Spark (ADR 0001, 0008); OpenAI profile available (ADR 0007) | `config/models.toml` |
| Benchmark | Retrieval suite ranks nodes with `bm25`, `vector`, `hybrid` (RRF, c = 60); recall@1/3/5/10 and MRR. Baseline not recorded yet | `docs/benchmarks.md`, `services/pipeline/benchmarks/` |
| Cluster | Two 4 GB nodes; Neo4j Community pinned to one, 2 GiB, 1 CPU | `infra/k8s/apps/neo4j/values.yaml` |

Three facts shape everything below:

1. **The graph is small and mostly structural.** The brandon-dev tenant is a few hundred to a few thousand nodes. At that size any of these methods runs in memory in milliseconds; scale is not the constraint, data is.
2. **Cross-source edges do not exist yet.** A graph method can only move relevance along edges that exist. Until phase 2 linking, graph propagation helps within a source (issue ↔ PR ↔ comments, message ↔ thread) and cannot connect a Slack thread to a Jira ticket.
3. **Two vector stores.** The benchmark ranks Neo4j nodes; the MCP tool ranks `source_records` rows. Both use the same model and roughly the same text, but a method chosen in the benchmark must be implementable against whichever store the product reads. Graph methods need the Neo4j side (or a copy of its edges).

## Candidates

### 1. Hybrid scoring

**How it works.** Run several independent rankers and fuse them. Ember already fuses BM25 and vector with reciprocal rank fusion ([Cormack, Clarke and Büttcher, SIGIR 2009](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)). The graph enters as a third signal: expand the top hits one or two hops along typed edges and either add neighbours as a ranked list to the fusion or rerank by graph distance from an anchor node. Graphiti's search does exactly this family of things: semantic, BM25 and breadth-first search, with RRF, MMR, node-distance (from a `center_node_uuid`), episode-mention and cross-encoder rerankers ([Graphiti search docs](https://help.getzep.com/graphiti/working-with-data/searching); [Zep paper, arXiv:2501.13956](https://arxiv.org/abs/2501.13956)).

**Cost.** No LLM calls. One query embedding plus a Cypher expansion. A cross-encoder reranker would add one model call per query and a second model on the Spark. Fits the cluster as is.

**What Ember needs.** Nothing new for within-source expansion. Edge-type weights (a `RESOLVES` hop is worth more than `CONTAINS`, which connects everything in a repository) are hand-set or tuned on the benchmark.

**What sets Ember's version apart.** The ontology fixes a small set of typed edges with known meaning, so the expansion can be type-aware instead of generic, and the `CONTAINS` hub problem can be handled by rule rather than discovered.

**Weakness.** A node two hops away with no lexical or semantic match is rarely promoted far enough to matter, and node → meaning is only "neighbours of this node".

### 2. Personalized PageRank from vector seeds (HippoRAG)

**How it works.** [HippoRAG](https://arxiv.org/abs/2405.14831) (Gutiérrez et al., NeurIPS 2024) builds a schemaless graph by LLM open information extraction over every passage, adds synonymy edges between phrase nodes whose embedding similarity exceeds 0.8, then at query time extracts entities from the question, maps them to the nearest graph nodes, and runs Personalized PageRank (PPR) restarting at those seeds (damping 0.5, seeds weighted by inverse passage frequency). Passages are scored from the resulting node probabilities. It reports single-step retrieval comparable to or better than iterative IRCoT while 10–30× cheaper and 6–13× faster. [HippoRAG 2](https://arxiv.org/abs/2502.14802) (ICML 2025) puts passages into the graph as nodes, matches the whole query to triples by embedding, has the LLM filter the top triples, and seeds every passage node with a small weight proportional to its query similarity. On MuSiQue it reports 9.2M indexing tokens and 1.2 s per query, against 68.5M / 13.3 s for LightRAG and 115.5M / 10.7 s for GraphRAG (Llama-3.3-70B; [HippoRAG 2 paper, efficiency table](https://arxiv.org/html/2502.14802v2)).

**Ember's version needs no extraction step.** Ember already has a typed graph from structured mapping, and every node has a vector. The adaptation is: seeds = top-k nodes by vector (or hybrid) score, restart weights proportional to those scores; PPR over the loader's edges; rank by the stationary distribution, optionally fused with the seed ranking. That keeps HippoRAG's mechanism and drops its main cost (LLM OpenIE over every item).

**Cost.** Zero LLM calls at index and query time in the base form. PPR on a few thousand nodes is a sparse matrix-vector loop, well under 100 ms in numpy. Neo4j GDS also offers PPR as `gds.pageRank` with weighted `sourceNodes` ([docs](https://neo4j.com/docs/graph-data-science/current/algorithms/page-rank/)), but GDS stores projections on the heap and Community GDS is capped at 4 cores ([system requirements](https://neo4j.com/docs/graph-data-science/current/installation/System-requirements/)); on a 2 GiB Neo4j pod it is simpler to read the edge list once and run PPR in the retrieval process.

**What Ember needs.** Edges with sensible weights, and above all cross-source edges: PPR can only reach what is connected. Two cheap sources of them, neither needing an LLM: (a) key mentions, e.g. `EMBER-39` in a PR title or Slack message becomes `REFERENCES` to the Jira `WorkItem` (already planned for phase 2), and (b) HippoRAG-style similarity edges, linking each node to its top few nearest neighbours above a threshold. (b) partly re-uses the vector signal, so its weight must be tuned, not assumed.

**What sets Ember's version apart.** Typed, deterministic edges instead of LLM-extracted triples, so the graph is reproducible and the walk can weight edge types; temporal validity (ADR 0002) can drop superseded facts from the walk.

**Both directions.** Question → graph: seeds from the question. Node → meaning: seed the walk at the node itself (and its vector neighbours), which gives "related by structure and meaning" with one mechanism.

### 3. Structure-aware embeddings (FastRP with text features, SGC, APPNP)

**How it works.** Compute, offline, a second vector per node that mixes its text embedding with its neighbours'.

- **SGC** ([Wu et al., ICML 2019](https://arxiv.org/abs/1902.07153)) shows that removing the non-linearities from a graph convolutional network leaves a fixed low-pass filter, i.e. repeated averaging of features over normalized neighbourhoods. Applied to text embeddings with no training: `X' = S^K X`, with `S` the normalized adjacency with self-loops and K = 1–2.
- **APPNP** ([Gasteiger, Bojchevski and Günnemann, ICLR 2019](https://arxiv.org/abs/1810.05997)) propagates with personalized PageRank instead, which keeps each node's own signal through a teleport term and avoids over-smoothing at larger depths. Used without its prediction network, it is "PPR-smoothed text embeddings".
- **FastRP** ([Chen et al., CIKM 2019](https://arxiv.org/abs/1908.11512); reported 4,000× faster than DeepWalk and node2vec) builds embeddings by very sparse random projection and iterated neighbour averaging. Neo4j GDS's implementation accepts `featureProperties` with a `propertyRatio` for the share of dimensions given to node properties ([GDS FastRP](https://neo4j.com/docs/graph-data-science/current/machine-learning/node-embeddings/fastrp/)).

**Cost.** No LLM calls. SGC/APPNP on Ember's graph is seconds of numpy per rebuild, no GPU. FastRP via GDS needs the GDS plugin on the 2 GiB pod (GDS 2026.09 is the version listed for Neo4j 2026.09; [compatibility matrix](https://neo4j.com/docs/graph-data-science/current/installation/supported-neo4j-versions/)), or a small reimplementation.

**The important difference.** SGC and APPNP outputs are linear averages of Qwen3 vectors, so they stay in the model's space and can be compared directly with a query embedding: they serve question → graph and node → meaning. FastRP's random projection leaves the text space; its vectors compare node with node only, so it serves node → meaning but not questions. ADR 0001's one-model rule still holds for SGC/APPNP (no second model), but the propagated vectors are derived data and must be recomputed whenever the graph or the base vectors change.

**What Ember needs.** Same edges as PPR; cross-source edges matter for the same reason. Storage for a second vector per node (Neo4j property or a pgvector column).

**What sets Ember's version apart.** Propagation can be restricted per edge type (smooth over `PART_OF` and `RESOLVES`, not over `CONTAINS` or `MEMBER_OF`), which generic implementations do not do.

### 4. A learned joint space (GNN with a contrastive objective)

**How it works.** Train a graph neural network (for example GraphSAGE, [Hamilton et al., NeurIPS 2017](https://arxiv.org/abs/1706.02216), which is inductive, so new nodes get vectors without retraining) to map each node and its neighbourhood into a space aligned with the text encoder. [ConGraT](https://arxiv.org/abs/2305.14321) (Brannon et al., TextGraphs at ACL 2024) trains a language model and a GNN with a CLIP-style batch contrastive loss on text-attributed graphs, self-supervised, so no hand labels. For Ember, the positives would be (question or node text, connected node) pairs: `RESOLVES` pairs, comment-to-issue, reply-to-parent, and later confirmed cross-source links.

**Cost.** Training on the Spark: minutes to hours for a graph this size, but it competes for a shared GPU (ADR 0008). Inference can be precomputed per node. Ops cost is the real one: a training pipeline, a model artifact with its own identity, versioning and re-training on graph change. Under ADR 0001 a fine-tuned text tower is a new embedding model and forces a full re-embed; keeping the text encoder frozen and training only the graph tower avoids that.

**What Ember needs.** Training data. A few hundred nodes are too few to learn a space better than the base encoder without memorizing the benchmark tenant. It becomes worth testing when synthetic data (EMBER-43) and labelled cross-source links (benchmark gaps 3 and 6) exist.

**What sets Ember's version apart.** The natural positives are domain links (PR resolves ticket, commit touches module), which is the code-to-ticket claim in benchmark gap 1. It is the method with the highest ceiling on that claim and the least support on today's data.

### 5. GraphRAG and LightRAG

**GraphRAG** ([Edge et al., arXiv:2404.16130](https://arxiv.org/abs/2404.16130); [microsoft/graphrag](https://github.com/microsoft/graphrag)) uses an LLM to extract entities and relationships from every text unit, clusters the entity graph with hierarchical Leiden, writes an LLM summary for every community, and answers "global" questions by map-reduce over the community summaries ([dataflow docs](https://microsoft.github.io/graphrag/index/default_dataflow/)). It targets corpus-wide sensemaking ("what are the main themes"), not finding one item. Its own README warns that indexing is expensive. LazyGraphRAG (Microsoft Research blog, 2024-11-25, [link](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/)) replaces LLM indexing with noun-phrase extraction and claims indexing at 0.1% of GraphRAG's cost, moving LLM work to query time.

**LightRAG** ([Guo et al., arXiv:2410.05779](https://arxiv.org/abs/2410.05779); [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG)) also extracts an entity-relation graph by LLM, then retrieves at two levels (specific entities and broader themes) combined with vectors, and supports incremental insertion without a rebuild. It has Neo4j and PostgreSQL backends and local/global/hybrid/naive/mix query modes; its README names Qwen3-30B-A3B-Instruct as a reasonable minimum for local extraction, which is what the Spark serves.

**Cost.** Both make LLM calls per item at index time (HippoRAG 2 measured 68.5M and 115.5M input tokens for LightRAG and GraphRAG on an 11.7k-passage corpus, versus 9.2M for HippoRAG 2) and one or more LLM calls per query (13.3 s and 10.7 s per query in the same table). On the shared Spark, re-indexing after every ingest batch would be the dominant load.

**What Ember needs.** Mostly nothing it has: both build their own schemaless graph from text, which duplicates the structured graph the loader already writes and ignores the ontology. Whether LightRAG can take a pre-built graph or a fixed entity-type list is not confirmed from its README (see Unverified).

**Fit.** These are answer generators, not node rankers, so they do not directly produce what the benchmark measures. They belong in Ember as **comparison systems** (benchmark gap 2); community summaries are a possible later feature ("what has the team been doing in area X"), not v2 search.

## Comparison

| Method | Question → graph | Node → meaning | LLM calls (index / query) | Training data | Cross-source edges needed | Ops on 2×4 GB + Spark |
| --- | --- | --- | --- | --- | --- | --- |
| Hybrid + graph expansion | Yes | Neighbours only | 0 / 0 (cross-encoder optional) | No | For cross-source gains | None new |
| PPR from vector seeds | Yes | Yes (seed at node) | 0 / 0 | No | Yes | In-process numpy, or GDS |
| SGC / APPNP vectors | Yes (same space) | Yes | 0 / 0 | No | Yes | Offline numpy, second vector column |
| FastRP with text features | No (different space) | Yes | 0 / 0 | No | Yes | GDS plugin on 2 GiB pod |
| Contrastive GNN | Yes | Yes | 0 / 0 | Yes, hundreds+ positive pairs | Yes (as positives) | Training job and model versioning on shared GPU |
| GraphRAG / LightRAG | Answers, not rankings | Weak | Per item / per query | No | Builds its own | Heavy, recurring Spark load |

## Recommendation for v2

1. **Keep `hybrid` (BM25 + vector, RRF) as the baseline** and record it first. No graph method should be adopted until the EMBER-58 baseline exists (`docs/benchmarks.md`, Baseline is still empty).
2. **Lead candidate: PPR from hybrid seeds over the structured graph** (HippoRAG mechanism, no LLM extraction). It serves both directions, costs nothing in model calls, runs in the retrieval process, and uses the ontology's edge types as weights.
3. **Second candidate: APPNP-smoothed text vectors** (SGC as the simpler control). They stay in the Qwen3 space, so the existing MCP `search` and HNSW index can use them unchanged as a second column; this is the cheapest way to put structure into the product path.
4. **Prerequisite, small: deterministic cross-source edges** from key mentions, plus an optional top-k similarity edge set. Without them, neither candidate can help on the cross-source questions that justify a graph.
5. **Defer** FastRP (no query direction), the contrastive GNN (no training data yet; revisit with EMBER-43 data and labelled links) and GraphRAG/LightRAG (answer generators; benchmark them as external systems under gap 2).

### How each candidate is scored in EMBER-58

The retrieval suite already compares `SYSTEMS` over the same docs and questions (`benchmarks/retrieval_suite.py`). Each candidate becomes one more system that returns a ranked list of node refs, so recall@k and MRR apply unchanged and results stay comparable within one data version and model profile.

| System name | Ranked list | Extra inputs |
| --- | --- | --- |
| `hybrid` | existing | none |
| `hybrid+expand` | RRF of bm25, vector, and 1-hop neighbours of the top 10 | edge list |
| `ppr` | PPR scores, seeds = top 20 of `hybrid` weighted by RRF score | edge list, damping, edge-type weights |
| `ppr+hybrid` | RRF of `ppr` and `hybrid` | as above |
| `appnp` / `sgc` | cosine of query vector to propagated vectors | edge list, K or teleport α |
| `gnn` (later) | cosine in learned space | trained model, held-out split |

Changes the benchmark needs:

- **Edges in the suite input.** `docs_from` loads node text and vectors only; graph systems also need the tenant's edges from the same load. Tuning parameters (damping, α, edge weights) must be fixed before the run or tuned on a separate question set, otherwise the score is fitted to the 20–30 questions.
- **Question tags.** Mark each question as single-source or cross-source and report scores per tag. Graph methods are expected to matter on cross-source and multi-hop questions; an overall number hides that.
- **A node → meaning suite.** Hold out a sample of true edges (`RESOLVES`, comment-to-issue, and labelled cross-source links once they exist), remove them from the graph before any propagation, and score how high each system ranks the held-out target given the source node. Without removing them first, structure-aware methods would score themselves on edges they were built from. This is also the shape of gap 1 (code-to-ticket with keys masked); the key-mention edges must be masked in the same test.
- **External systems.** GraphRAG and LightRAG are scored through the gap 2 adapter ("answer a question, return the items you used"), mapping the chunks they cite back to node refs, and labelled as answer systems with their LLM and token counts in the result.

Adoption rule (proposal): a graph system replaces `hybrid` only if it improves MRR on cross-source questions without lowering recall@10 on single-source ones, on the same data version and models, and the gain survives a second question set.

## Unverified or not checked

- LightRAG accepting a pre-built graph or a fixed entity-type list: not stated in its README; not checked in the code.
- GDS memory needs on a 2 GiB Neo4j pod: GDS keeps projections on the heap, but no estimate was run for Ember's graph.
- Latency figures for in-process PPR and SGC/APPNP on Ember's graph are estimates from graph size, not measurements.
- The HippoRAG 2 cost table comes from that paper's own runs (Llama-3.3-70B, MuSiQue); costs on Qwen3-30B-A3B and Ember's data will differ.
- LazyGraphRAG's 0.1% indexing-cost figure is Microsoft's claim in a blog post, not a peer-reviewed result.
- GraphSAGE details are cited from the paper's well-known abstract and were not re-read for this document.

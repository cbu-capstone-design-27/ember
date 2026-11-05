# Unified search experiment workflow

EMBER-132; implementation starts with EMBER-133/134/135 and the fixed variants of EMBER-137/138/139/140.
This follows [ADR 0011](../adr/0011-graph-in-postgres.md): keep the Neo4j reference until the
Postgres Graphiti fork demonstrates parity. Vertex AI replaces the tunnel for models (EMBER-155/116).

## First implementation

`services/pipeline/benchmarks/unified/` is an offline experiment package. It calls no cloud service or
database and makes no production graph writes. The export adapter replays photon source rows through the
existing extractor and ordered in-memory writer. Its output has 1,140 current, non-placeholder nodes,
2,327 edges and 778 searchable records. Another store can supply the same JSON contract later.

The graph contains a tenant, typed source-qualified references, current text, URLs, source times, typed
edges and one embedding identity. Deletions, unresolved placeholders and other tenants are excluded.
Its fingerprint includes content, vectors, edges and identity. Raw text embeddings can be reused by
identity plus text hash; a vector whose stored text hash is stale is excluded. Vectors derived from
neighbours are rebuilt after masking held-out links.
The in-memory cache is injectable; persistent/incremental production vector storage is still outstanding.
Exports contain content and must stay local for real team data. Aggregate result JSON omits document
references, text and queries. Only synthetic photon fixtures and candidate questions are committed.

The experimental mention resolver adds `REFERENCES` evidence for unique Jira keys, qualified GitHub
numbers, GitHub issue/PR URLs, allowlisted Jira URLs and allowlisted Slack permalinks. Bare `#n` needs
a GitHub repository context. It reports ambiguous/missing targets rather than guessing. It never creates
`FIXES` or other causal relationships from a mention. It is not yet the durable production linker:
remote-link fields, replay/order/deletion integration, placeholders and the human precision audit remain.
On photon the default scan reports 36 resolved mentions, 529 unloaded targets and 520 bare numbers
without repository context. Much of photon GitHub content is a Slack bot feed; GitHub target nodes are
absent from that fixture. These are evidence counts, not precision or unique-edge counts.

## Run locally

Install `services/pipeline/requirements.txt` in a virtual environment. From `services/pipeline`:

```sh
# BM25 against the synthetic fixture. The bank is explicitly an unreviewed validation draft.
python -X utf8 -m benchmarks.unified --allow-draft --link-mentions \
  --output ../../data/unified-bm25-draft.json

# Exercise every implemented path without a provider. This is not semantic quality evidence.
python -X utf8 -m benchmarks.unified --allow-draft --link-mentions --diagnostic-bag \
  --methods bm25 vector hybrid fusion ppr smoothed structural \
  --output ../../data/unified-diagnostic.json --save-graph ../../data/photon-diagnostic-graph.json

python -X utf8 -m unittest discover -s benchmarks -t . -p "test_*.py" -v
```

`-X utf8` makes the existing photon seed reader portable to Windows. Outputs belong under ignored
`data/`. Real-data inputs must be prepared separately using the already approved read-only/local-copy
workflow. This command does not fetch them. `--graph` accepts a `Graph.save()` export. `--queries` accepts
`{"identity": "<exact export identity>", "vectors": {"question text": [0.1, 0.2]}}` produced by the same
raw text embedding model. The model identity should include provider, project/location where applicable,
model/revision, dimensions, query/document task choices and any prefix template. Query and document
identities must match; dimensions, cardinality, non-finite and zero vectors are rejected.

The new CLI does not add another model client. Vertex-produced vectors will come through the provider
work in EMBER-116/155 or an explicit adapter using that client after it lands. Imported embeddings have
unknown embedding latency/tokens, reported as null. Per-query latency is retrieval only; preparation and
per-system graph build time are separate. Do not compare it with end-to-end timings without adding the
provider cost. LLM call/token fields cover the retrieval plugin, not unknown upstream embedding work.

## Comparison contract and leakage controls

An experiment implements `retrieve(question, tenant, k) -> Result(refs, costs)` and
`related(anchor_ref, tenant, k) -> Result(refs, costs)`. `evaluate()` takes a registry of factories accepting
the masked graph and shared query vectors. It refuses candidates outside the exported corpus, duplicate
rankings, mismatched query models and out-of-tenant requests. Production EMBER-119 additionally needs
source filtering, newest-version semantics and the MCP result fields; the experiment API does not claim
to replace that implementation.

Every question has stable ids, source-qualified answer references, category, split, leakage family,
review status, and optionally a related-item anchor and hidden endpoint pairs. All hidden pairs from
the **whole bank** are removed in both directions, across every edge type, before constructing any
system or deriving neighbour/structural vectors. Raw document text remains visible to every system.
Changing hidden pairs changes the masked graph fingerprint. There is no cached graph-derived feature
reuse across masking configurations. The runner cannot validate the provenance of arbitrary imported
vectors: callers must export raw text vectors, never previously graph-smoothed vectors.

Question ids and normalized texts must be unique. A leakage family cannot cross splits. A person must
assign families by connected topic/episode, including paraphrases and shared evidence; the validator
cannot infer that semantic grouping. The 60 candidate questions are all unreviewed, validation-only:
40 single-source, 10 cross-source and 10 related-item. Relevance judgments are candidates and may be
incomplete. Reviewers must remove easy copies/near duplicates, check complete relevant sets and add
genuinely multi-hop and no-answer cases before allocating a sealed test set. They do not fulfill the
human-authored requirement in EMBER-58. Test evaluation requires `--split test --unlock-test` and every
selected question reviewed; simulated embeddings are prohibited for test runs.

Scores use true set recall@5/10, MRR@10, binary nDCG@10 and retrieval latency p50/p95, separately by
category and overall. The legacy EMBER-58 metric called recall measures first-hit success; it is preserved
for compatibility and must not be compared numerically as set recall. New results include fingerprints,
model identity, split, review state, graph build time, and retrieval LLM calls/input/output tokens.
Later plugins must report actual usage rather than returning the non-LLM defaults.

## Implemented fixed variants

| System | First variant | Remaining ticket work |
|---|---|---|
| `bm25`, `vector`, `hybrid` | Existing lexical/cosine/RRF controls on the identical corpus | Reviewed questions and Vertex baseline |
| `fusion` (137) | Rerank 50 hybrid candidates, 0.7 normalized cosine + 0.3 proximity to five seeds, at most two hops | Typed/learned weights and ablations |
| `ppr` (138) | Five hybrid seeds, harmonic seed weights, restart 0.2, dangling mass returned to seeds | Typed edge weighting, seed/restart validation sweep |
| `smoothed` (139) | Two symmetric normalized self-loop propagation steps; restart 0.5 to raw text vectors | Typed weights, depth/restart sweep and source ablations |
| `structural` (140) | Seeded 32-D random projection diffusion; query graph position borrowed from top-five hybrid hits; 0.7 semantic + 0.3 structural cosine | Actual FastRP/node2vec comparison and parameter sweeps |

The structural variant is a small deterministic diffusion control, **not an implementation of FastRP
or node2vec**. For graph walks/features, edges are currently undirected with equal weights and only
content endpoints participate. Person, Container and other hubs are excluded. These choices are explicit
initial controls, not tuned or adopted settings. Related-item retrieval uses the anchor's text/vector
and excludes the anchor from results. Its requested natural-language question is descriptive only.

The optional photon source-time cutoff excludes future rows before replay. The seed supplies only
the latest Jira issue records, so this cannot reconstruct older Jira properties or status history
that is absent from those rows. It provides safe temporal experiment plumbing, not a complete
historical graph or a measured temporal retrieval method. Changelog-based validity remains separate.

## Work across all assigned tickets

| Ticket | Current work / next dependency |
|---|---|
| EMBER-58 | Draft questions prepared here; human review/real baseline still required. Preserve its acceptance criteria. |
| EMBER-84 | Research PR #82 remains for team review; use this workflow to produce measured follow-up evidence. |
| EMBER-119 | Keep the public MCP interface stable; agree backend/result mapping with Ryan, then integrate Graphiti reference. No production replacement is in this PR. |
| EMBER-132 | Track all approaches and their dependencies, with no selected winner yet. |
| EMBER-133 | Experiment mention evidence built; durable ordered linker, remote links, deletions and audited precision next, coordinated with Phase A. |
| EMBER-134 | Shared harness, metrics, plugin registry and 60-question draft built; review/splits, confidence estimates and genuine baseline next. |
| EMBER-135 | Typed export, identities/fingerprints and incremental raw-text cache built; Vertex vectors and store adapters next. |
| EMBER-136 | Group/split isolation exists in the harness; generate the training-only synthetic corpus after reviewed held-out families are fixed. No synthetic training data generated yet. |
| EMBER-137–140 | Fixed variants above built; quality runs, sweeps and missing variants remain after baseline. |
| EMBER-141 | Learned graph/text towers after 135/136; train/validation only, frozen test families. No model trained yet. |
| EMBER-142 | Community-summary retrieval after Vertex access; build its own summaries on the same masked graph, measure build and retrieval cost. Not implemented yet. |
| EMBER-143 | Read-only graph query/traversal adapter after the stable backend/export contract. Target SQL for the planned Postgres store; retain Neo4j only as the temporary reference. Not implemented yet. |
| EMBER-144 | Cross-encoder plugin over identical candidate pools; include rerank latency and source slices. Not implemented yet. |
| EMBER-145 | Photon export can replay as of source time, with future records excluded. Add versioned temporal relevance judgments and compare with Graphiti temporal search; no temporal winner measured. |
| EMBER-146 | Stock Graphiti harness and Vertex adapter PR #121 already exist; real Vertex/Neo4j sample and reference-quality run remain. |
| EMBER-155 | PR #121 passed 15 CI checks but remains draft pending real Vertex validation. Jacob-owned cutover/pruning checks remain in its runbook. |

The remaining external inputs are Vertex project/location/model/dimensions plus ADC authorization, a
working local Docker runtime for the Neo4j reference, human-reviewed relevance judgments, and agreement
with Ryan on the production retrieval interface/Phase A ownership. Infrastructure changes stay with
Jacob; this experiment does not deploy, delete the tunnel or remove Neo4j.

Adoption still requires better cross-source results without degrading single-source results, with
paired uncertainty estimates and acceptable latency/cost. Diagnostic or unreviewed numbers cannot
pass that gate. Graphiti, direct loading or a hybrid remain open outcomes.

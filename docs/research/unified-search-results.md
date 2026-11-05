# Unified search results

EMBER-132/134. No retrieval method has been selected for adoption.

| Run | Corpus / judgments | Embeddings | Evidence |
|---|---|---|---|
| Foundation diagnostic, 2026-11-04 | photon, 778 records; 60 agent-authored, unreviewed validation questions; ten held-out pairs removed | Simulated 64-D bag-of-words | All seven implemented systems execute and emit category metrics. This provides no evidence of semantic search quality. |
| Vertex baseline | Pending human review and provider access | Pending | No score recorded |
| Graphiti reference | Fake-model Neo4j compatibility already recorded in its existing report; real Vertex run pending | Pending | Compatibility is not retrieval quality |

The diagnostic's detailed aggregate JSON and content/vector export stay under ignored `data/`.
Do not rank methods using this run. The test suite checks probability mass, graph masking before
feature derivation, model identities, isolation, true set recall and rejection of invalid candidates.

Reproduction, remaining variants, the complete ticket plan and cost/latency definitions are in
[the experiment workflow](unified-search-workflow.md). Add comparable validation/test rows only after
recording the corpus/question fingerprints, exact model identity, review state and measured provider cost.

# ADR 0001: Embedding model

- Status: Accepted
- Date: 2026-09-29
- Jira: EMBER-33

## Context

Ember embeds text in two places: the processing pipeline embeds what it writes to the graph, and the retrieval endpoint embeds the agent's prompt at query time. Similarity only means something when both sides use the same model, so the model is a shared, long-lived choice. Switching later invalidates every stored vector.

The model has to run on the DGX Spark cluster, be usable in a self-hosted deployment, and carry a license compatible with Ember's Apache 2.0. We also expect to move to a hosted model (Vertex AI or similar) for embeddings and the LLM later, so the choice should be cheap to replace.

## Decision

Use **Qwen3-Embedding-0.6B** (Apache 2.0) as the launch embedding model.

From the model card:

- 1024-dimension output (variable output sizes from 32 to 1024 are supported; Ember uses the full 1024).
- 32k-token context.
- Last-token pooling.
- Queries take an instruction prefix, `Instruct: <task description>\nQuery: <query>`. Documents are embedded with no prefix.

Serving:

- One shared embedding service on the DGX Spark exposes an OpenAI-compatible `/v1/embeddings` endpoint. The pipeline and the retrieval endpoint both call it. Nothing else loads the model.
- The query instruction text is part of the model's identity. It is recorded alongside the model and changing it counts as switching models.

### Rule: one model per deployment; switching requires a full rebuild

A deployment picks one embedding model and keeps it. Vectors from different models are never mixed in one graph, and the dimension is configuration, not a hard-coded constant.

To switch models, re-embed every stored vector and rebuild any vector index, then cut over. There is no partial or gradual migration, and no similarity search across the boundary. A deployment means one graph plus the services that read and write it, so the hosted deployment and each self-hosted deployment may differ, but each stays internally consistent.

Each deployment records the model id, revision, dimension, and query instruction template with its graph. Services that embed or query check them at startup and refuse to run on a mismatch.

## Consequences

- A planned move to a hosted embedding model is a deliberate, scripted rebuild, not a config flip.
- To keep a rebuild cheap, every embedded item stores the exact text that was embedded (`embed_text`), and raw payloads stay in the data lake. A rebuild re-embeds stored text and does not re-parse or re-extract.
- The retrieval endpoint and the pipeline must apply the same conventions: instruction prefix on queries only, documents unprefixed, same dimension.
- The startup check needs the graph schema (EMBER-34) to provide a place to record the config. Until it does, the recorded values live in the embedding service's README.
- A small model is fast on the Spark and runs on modest hardware in self-hosted setups. We accept lower ceiling quality than the 4B and 8B models in the same family in exchange for that and for a cheap rebuild.
- Choosing an open-weights model keeps project text on hardware the deployment controls, which matches the self-hosting promise.

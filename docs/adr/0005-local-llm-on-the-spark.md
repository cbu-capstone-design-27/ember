# ADR 0005: Local LLM and embedding on the DGX Spark until cloud credits arrive

- Status: Accepted (supersedes ADR 0004's default profile for now; ADR 0004's models stay available)
- Date: 2026-10-13
- Jira: EMBER-49

## Context

ADR 0004 chose hosted OpenAI models for development. Cloud credits are still being arranged
(purchasing form: EMBER-60), so the pipeline runs on models we host ourselves on the shared DGX
Spark until they arrive. ADR 0001 already runs the embedding model there.

## Decision

- Chat: **Qwen3-30B-A3B-Instruct-2507-FP8** (Apache 2.0), served by vLLM as `qwen3-30b-a3b-instruct`,
  revision `5a5a776300a41aaa681dd7ff0106608ef2bc90db`, with a 32k context.
- Embedding: unchanged, Qwen3-Embedding-0.6B (ADR 0001).
- Both are selected by the `local-spark` profile in `config/models.toml`, which is now the default.
  `openai-dev` stays in the file, so moving to a hosted model later is `MODEL_PROFILE=openai-dev`
  and a key, not a code change. As ADR 0001 requires, a graph that already holds embeddings from one
  model refuses another, so switching embedding models on an existing graph is a deliberate re-embed.

Why this model:

- It is a mixture-of-experts model with about 3B parameters active per token. The Spark has a lot of
  memory (128 GB, shared) but modest memory bandwidth, and decoding speed is bound by bandwidth, so a
  3B-active model is several times faster than a dense 8B model while having 30B parameters of knowledge.
- The 2507 Instruct release has no thinking mode. It answers directly, so the extraction work does not
  burn tokens on hidden reasoning, and the client needs no model-specific switch.
- FP8 weights are about 31 GB, which leaves room for the embedding service and for other people's
  jobs on the shared host. The GPU share is capped with `--gpu-memory-utilization` (0.35 here, 0.10 for
  embeddings).
- Apache 2.0 matches Ember's license and keeps project text on hardware we control.

What was not chosen: a dense Qwen3-8B (slower here, weaker), Qwen3-4B-Instruct-2507 (the fallback if the
30B model does not fit beside other jobs), and the Qwen3-VL models already cached on the host (vision
models we do not need).

## Network

- The Spark cannot join Tailscale. The embedding service is on the Spark's LAN address that maps to the
  public IP (port 8000), with a bearer key. The cluster nodes that run the pipeline and Neo4j are the
  ones reached over Tailscale.
- The chat model binds to loopback (`LLM_BIND_ADDR`) and is reached through an SSH tunnel
  (`ssh -L 8001:127.0.0.1:8001 shared-dev`). Both servers require a bearer key (`VLLM_API_KEY`).
- Decided (2026-10-13): the pipeline on a cluster node reaches the chat model through the same kind of
  SSH tunnel, started on the node and kept running by a supervisor (systemd or autossh), pointing at the
  Spark's existing SSH port. The node holds an SSH key as a managed secret. The pipeline then uses the
  profile's default `http://localhost:8001/v1`. Nothing new is exposed and no firewall change is needed.
- Not chosen: opening public port 8001 with a key (a 30B chat endpoint is a more attractive target than
  embeddings, and the key would cross the network in plain HTTP), and a reverse proxy on port 8000 (keeps
  one open port but still publishes the model over plain HTTP). Revisit the proxy if a direct route is
  needed later.

## Consequences

- Scores from this model are not comparable with scores from hosted models. EMBER-58 labels every result
  with the model used.
- The Spark is shared. If another job needs the GPU, stop the service (`docker stop ember-llm`) or lower
  `LLM_GPU_MEMORY_UTILIZATION`.
- Deploying or refreshing: `infra/spark/deploy-llm.sh` (the embedding service has `infra/spark/deploy.sh`).

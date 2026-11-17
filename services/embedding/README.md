# Embedding service

The single embedding model for Ember (ADR 0001, EMBER-33). It runs on the shared DGX Spark, not on the k3s cluster, and serves an OpenAI-compatible `/v1/embeddings` endpoint on one open port, protected by an API key. The pipeline and the retrieval endpoint are its only clients.

## Model identity

These values are the deployment's embedding identity. Changing any of them is switching models and requires a full rebuild (see below).

| | |
| --- | --- |
| Model | `Qwen/Qwen3-Embedding-0.6B` (Apache 2.0) |
| Revision | `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` |
| Dimension | 1024 |
| Pooling | last token |
| Served name | `qwen3-embedding-0.6b` |
| Query template | `Instruct: {task}\nQuery: {query}` |
| Document template | none (raw text) |
| Default task | `Given a question about a software project, retrieve relevant passages that answer it` |

Queries get the template; documents never do. The template and default task are part of the identity: editing them counts as switching models.

## Rule: one model per deployment

A graph holds vectors from exactly one model. To switch (including moving to a hosted model), re-embed every stored `embed_text`, rebuild any vector index, then cut over. Never mix vector spaces or run similarity across the boundary. Services that embed or query must compare the values above with what the graph records and refuse to start on a mismatch. The graph-side record is defined by EMBER-34; until it lands, this table is the record.

## Running it (Spark)

Compose profile `embedding` in `infra/docker-compose.yml`. Image: `nvcr.io/nvidia/vllm:26.02-py3` (NVIDIA's arm64 build for the GB10).

```bash
cd ~/ember
cp .env.example .env
# set EMBEDDING_API_KEY (openssl rand -hex 32) and EMBEDDING_BIND_ADDR=192.168.94.11
docker compose -f infra/docker-compose.yml --env-file .env --profile embedding up -d embedding
```

The endpoint binds to `EMBEDDING_BIND_ADDR`, which defaults to loopback. On the Spark it is the LAN address that the public IP maps to; do not use `0.0.0.0`, which would also expose the Spark's internal interfaces. Clients must send `Authorization: Bearer <key>`.

Reaching it from the cluster: clients call `http://<public-ip>:8000` (the port must be forwarded to the Spark by whoever runs the network). Traffic is plain HTTP, so the API key crosses the network unencrypted. Treat the key as low-trust: rotate it by changing `EMBEDDING_API_KEY` and recreating the container, and put TLS in front (a reverse proxy) before sending anything sensitive.

The Spark is shared. The service reserves a small slice of GPU memory (`EMBEDDING_GPU_MEMORY_UTILIZATION`, default 0.10) and does not use host ports other than `EMBEDDING_PORT`.

## Checking it

```bash
curl -s http://<spark-public-ip>:8000/v1/embeddings \
  -H "Authorization: Bearer $EMBEDDING_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-embedding-0.6b","input":["hello world"]}' \
  | python -c "import sys,json; print(len(json.load(sys.stdin)['data'][0]['embedding']))"
```

Expected output: `1024`.

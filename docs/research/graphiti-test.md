# Stock Graphiti on Neo4j with the photon data

Jira: EMBER-146, step 1. Code: `services/pipeline/graphiti_test/`.

The team means to move the graph to Postgres with a thin Graphiti fork. Before that, this harness runs **stock** `graphiti-core==0.31.2` on Neo4j with our data and writes a reference the fork must later match. It also serves as the spike in [ADR 0002](../adr/0002-graph-engine-and-schema-approach.md).

**Status:** verified end to end against a local fake model only. Nothing here says anything yet about extraction quality, retrieval quality or real cost. The real run is below.

## What one run does

    python -m graphiti_test run --tenant photon --limit N --profile <profile> --out <dir>

1. Reads the rows `apps/mcp/seed_photon.py` builds, without Postgres, and sorts them by `occurred_at`. `--limit` keeps the records the questions need first, then the earliest others. 16 or more holds them all.
2. Passes `ontology.ENTITY_TYPES`, `EDGE_TYPES` and `EDGE_TYPE_MAP` to `add_episode`, with `group_id = ontology.subgraph_id(tenant, source)`, one row at a time.
3. Records each episode's LLM calls by prompt, HTTP requests, tokens, embedding calls, wall time and failures in `<dir>/episodes.jsonl`. Rerunning with the same `--out` resumes: done rows are skipped, failed rows are retried, and an episode already in the graph is not added twice. Five failures in a row stop the run.
4. Runs 9 questions (EMBER-78's five and four EMBER-86 shapes) through hybrid search under 5 rerankers: `rrf`, `mmr`, `cross_encoder`, `node_distance` (centred on that question's top `rrf` node) and `episode_mentions`. EMBER-86's fifth shape, subgraph isolation, is a direct count of crossing edges.
5. Asks "what was true then": Photon's release stack as of before and after the 2026-09-15 rollback from Rust + GPUI to Swift, and lists every fact Graphiti ended. Photon has no edited records, so this decision is its one clear change of fact.
6. Re-sends one episode (ADR 0002 criterion 2) and reports what it added.
7. Writes `<dir>/reference.json` and `<dir>/summary.md`.

`--fake` starts the local fake. Both files then say the output is not meaningful for quality or cost. `--reset` deletes the tenant's subgraphs and the ledger first. The harness refuses a Neo4j on a cluster host, because it adds Graphiti's indexes and data to whatever it is given. It sets `GRAPHITI_TELEMETRY_ENABLED=false`.

## The reference format

`reference.json` (`format: ember-graphiti-reference/v1`) has two halves:

- `comparable`: the graph by type, what search returns, the temporal and resend results, all by natural identifiers (names, facts, record ids). No uuids, times or scores.
- `measured`: engine versions, models, cost, latency, indexes and server notifications. Differs on every run.

To compare two runs:

    python -m graphiti_test diff first/reference.json second/reference.json [--ranking]

It reads only `comparable`, and exits 1 on any difference except result order within one search, which it counts apart (`--ranking` lists them). Order moves on its own: BM25 ties break on internal document order, and that shifts rank fusion's scores. `in_top3` is compared. A tie across third place could in principle flip it between identical runs, though none has in the runs so far.

For the fork: `search_suite.py` calls only `graphiti.search_`, so it runs unchanged. All the harness's Cypher is in `graph_reader.py`, so the fork needs its own reader with the same methods, plus its own driver where `run.py` builds `Neo4jDriver`.

## Verified so far (fake models)

All on 2026-11-04 with `graphiti-core` 0.31.2, `neo4j` driver 6.4.1, `openai` 2.54.0, Python 3.12, all 778 photon records (112 issues, 29 comments, 637 messages).

| | `neo4j:5.26-community` (5.26.32) | `neo4j:2026.09.0-community` (2026.09.0) |
| --- | --- | --- |
| **Stock Graphiti works** | **Yes** | **Yes** |
| Episodes ingested | 778 of 778 | 778 of 778 |
| Indexes | 33 online, 0 errors | 33 online, 0 errors |
| Cypher errors | 0 | 0, and 0 errors or deprecations in the server log |
| Server notifications | `01N52` "property key does not exist", until the first data has that property. Harmless | None |
| Searches | 41 run, 0 errors, 4 skipped | 41 run, 0 errors, 4 skipped |
| Temporal check | Old fact ended, visible as of before, gone as of after | Same |
| Crossing edges | 0 | 0 |

- `python -m graphiti_test diff` between the versions finds **no differences beyond result order**: the graphs match type by type, and every `in_top3` matches. Result order differs in 20 places between the versions and in 26 places between two runs on 2026.09, so that is noise, not a version effect.
- The 4 skipped searches are `node_distance` on questions whose `rrf` run found no node to centre on.
- The cluster's chart is `2026.9.0` (`infra/k8s/apps/neo4j`). This was run on the Docker image of that version, Community edition as on the cluster, but not on the cluster.
- Resume, the ledger, counters, the reference format and failure recording have tests. An episode whose model answers null for a required attribute fails, is recorded as `structured_output`, writes nothing, and the run goes on.

The `EquivalentSchemaRuleAlreadyExists` errors in the run's log are stock Graphiti creating indexes concurrently. It ignores them, though its driver wrapper logs them as errors.

## What was found in stock Graphiti

From running it, unless marked as code reading.

1. **A plain install does not import.** `graphiti-core` asks for `openai>=1.91` and imports `httpx` without declaring it. `openai` 3.x moved to `httpx2`, so `pip install graphiti-core==0.31.2` today ends in `ModuleNotFoundError: httpx`. `graphiti_test/requirements.txt` pins `openai==2.54.0`, the last on `httpx`. The fork should declare `httpx` itself, and this is a small upstream fix.
2. **Telemetry is on by default.** Every `Graphiti()` posts to PostHog unless `GRAPHITI_TELEMETRY_ENABLED=false`. Any deployment must set it. The fork could change the default. (Code reading; the harness always turns it off.)
3. **A null for a required attribute fails the whole episode.** `SourceRef` requires `source`, `kind` and `external_id`. Graphiti's attribute prompt tells the model to answer null for what it cannot find, and then validates, so one such node loses the episode and nothing retries it. Episode text carries those ids to give the model a fair chance. How often this happens on a real model is the first thing to read from the real run. If often, those fields need to become optional for Graphiti, or to come from the deterministic path as research Part 2 proposes.
4. **`RELATES_TO` means two things.** `add_episode` does not enforce `edge_type_map`: it has no way to pass `strict_edge_types`, so an edge outside the map is renamed to Graphiti's default, `RELATES_TO`, and kept. That is also our ontology's name for WorkItem to WorkItem. The fake run shows 36 such edges. `graph.*.edges_off_the_ontology_map` counts them. The fork or the ontology has to separate the two names.
5. **The stock cross-encoder is tied to OpenAI, and costly.** `OpenAIRerankerClient` biases two OpenAI token ids and ranks by logprobs. Those ids mean something else to Qwen3, so its ranking there is unproven (code reading). A server that returns no logprobs raises `ValueError`, which is tested. It makes one LLM call per candidate passage: median 21 and up to 37 per search on the fake. `BGERerankerClient` needs `sentence-transformers` and a model download, so it was not run.
6. **`OpenAIGenericClient` does not feed Graphiti's token tracker**, so the harness counts at the HTTP layer. It also does not validate against the schema: bad JSON and empty replies retry (4 attempts, 5 to 120 s apart, plus the OpenAI SDK's own 2 retries), but a reply off the schema fails with no retry (code reading).
7. **Query prefix: ADR 0002 is out of date.** `search_` now takes `query_vector`, so the harness embeds queries with the profile's `query_template` and needs no fork for it. Documents stay unprefixed, as ADR 0001 wants.
8. **The embedder** never sends `dimensions` (the harness's small subclass adds it where the profile says), slices longer vectors, and does not sort by `index` (code reading). `models/fake_server.py` returns rows out of order on purpose, which is one reason it is not reused here.
9. **`add_episode(uuid=...)` looks an episode up**; it cannot set a new one's id, so there are no deterministic episode ids on this path (code reading). Graphiti has no idempotency key: a re-sent episode adds an `Episodic` node each time. With the fake it adds no entities or edges, but real wording can vary. Ember has to stop re-sends upstream, as the loader does.
10. **`group_id`** must match `^[a-zA-Z0-9_-]+$` (code reading, and tested on `ember-development_slack`). `<tenant>_<source>` passes unless a tenant slug has other characters.

## LLM calls per episode

From reading `add_episode` in v0.31.2:

| Prompt | Calls |
| --- | --- |
| `extract_nodes.extract_{message,json,text}` | 1 |
| `dedupe_nodes.nodes` | 0 or 1: only if a name is not resolved without the LLM |
| `extract_edges.edge` | 1 |
| `dedupe_edges.resolve_edge` | Up to 1 per extracted edge: skipped with nothing to compare, or on a verbatim match |
| `extract_edges.extract_timestamps` | Up to 1 per extracted edge the model left undated |
| `extract_edges.extract_attributes` | Up to 1 per extracted edge whose type has fields: only `REVIEWED` and `RELATES_TO` |
| `extract_nodes.extract_attributes` | 1 per node of a type with fields: all nine of ours |
| `extract_nodes.extract_summaries_batch` | 0, or 1 per 30 nodes whose summary has grown past about 2,000 characters |

So about `2 to 3 + nodes + 1 to 3 x edges`, plus summaries. Four nodes and three edges is about 13. Embeddings: about 4 requests.

The fake's measured numbers match that shape: median 6, range 3 to 20, 6,087 calls over 778 episodes; 2,701 embedding requests. **These follow the fake's extraction, not a model's**, and a real model will likely find more per episode. Its token counts are word counts.

Attribute extraction is the largest single share of calls (1,911 of 6,087). With all 778 episodes, even 13 calls each is about 10,000 LLM calls, before search.

## What still needs real models

| ADR 0002 criterion | Where to read it | Status |
| --- | --- | --- |
| 1. Entities and edges land on declared types | `graph.*.entities_by_type`, `edges_off_the_ontology_map` | Needs real models. Measured path works |
| 2. A PR closing an issue links to it; a re-send makes no duplicates | `resend` | Re-send needs real models. **PR to issue is not covered: photon has no GitHub records** |
| 3. Retrieval over entities and facts answers the questions | `search[].in_top3` | Needs real models |
| 4. Ingest cost and latency per event are tolerable | `measured.episodes`, `structured_failures` | Needs real models |
| 5. Attributes land as node properties, so `schema.cypher` applies | `entities_missing_source_ref` | Properties do land. **`schema.cypher` was not applied**: its uniqueness on `(group_id, source, external_id)` depends on what the model gives as `external_id` |

ADR 0002 also names the five fixtures in `packages/ingestion-envelope/fixtures/`. This harness uses photon only.

Also unverified, each read from the code and only answerable on the Spark:

- **Prompt size.** Graphiti sends `max_tokens=16384` and the chat model has a 32k context, so a prompt over about 16k tokens should get an HTTP 400 from vLLM, recorded as a failure. It lands in `other`, not `structured_output`. If it happens, lower the output limit first.
- **`sim_min_score` of 0.6** on cosine search and on invalidation candidates was not chosen for Qwen3. If real scores run lower, vector hits vanish without an error, and contradictions go unnoticed.
- **json_schema on vLLM.** `--structured-mode json_object` is the fallback.
- **Logprobs and `logit_bias` on vLLM** for the cross-encoder. A failure is recorded per question and does not stop the run.
- **`encoding_format`.** The OpenAI SDK asks for base64 embeddings by default. The fake ignores that and returns floats, which the SDK accepts. The Spark's vLLM may behave differently.
- **`openai-dev`** goes through `OpenAIClient` (the Responses API), since gpt-5 models reject the `max_tokens` the generic client sends. It has not been run and is paid.

## The real run on Vertex AI (EMBER-155)

The test now has `--vertex` for stock Graphiti's native Gemini extraction and relevance reranker, plus a native
Vertex embedder. The production embed worker and MCP embedding provider remain EMBER-116 (Ryan).
This change prepares the test; **no real Vertex quality/cost results have been measured yet**.

Use Google Application Default Credentials (ADC), with permission to invoke the chosen models on the billing
project. For a local user, authenticate with `gcloud auth application-default login`; workload credentials can
use `GOOGLE_APPLICATION_CREDENTIALS` pointing to an ignored credential configuration. SDK-managed ADC refreshes
access tokens, so do not paste a short-lived token into `LLM_API_KEY`. This path uses standard Vertex AI with a
project and location, not the Gemini Developer API or Vertex express API keys.

Set these non-secret variables explicitly; there are no model or dimension defaults to accidentally choose
for the team:

| Variable | Value |
| --- | --- |
| `GOOGLE_CLOUD_PROJECT` | Approved project with billing and the Vertex AI API enabled |
| `GOOGLE_CLOUD_LOCATION` | Region supporting both selected models (or `global` where supported) |
| `VERTEX_LLM_MODEL` | Chosen Gemini model ID supporting structured output |
| `VERTEX_EMBEDDING_MODEL` | Chosen text embedding model supported by the native Google Gen AI SDK |
| `VERTEX_EMBEDDING_DIMENSIONS` | Positive output dimension supported by that model |

With the harness requirements installed and a **fresh local throwaway Neo4j** as below, start with:

    python -m graphiti_test run --vertex --tenant photon --limit 16 --concurrency 1 --out ../../data/out/graphiti-vertex-sample

Inspect `summary.md` and failures before the full run. For the full reference, use a fresh graph or reset the
sample graph, and a separate output:

    python -m graphiti_test run --vertex --tenant photon --reset --concurrency 1 --out ../../data/out/graphiti-vertex

The native path sets `RETRIEVAL_DOCUMENT` for indexed text and `RETRIEVAL_QUERY` for benchmark questions, requests
the configured output dimension, rejects invalid vectors, and normalizes them. Internal Graphiti entity/fact
matching continues using the document embeddings. The query template is plain `{query}`, without Qwen prefixes.
Each embedding request contains one text, avoiding model-dependent batch limits. The reference records the
project, location, models, dimensions, task settings and SDK version. `model-identity.json` prevents resuming the
same output with another model, including a different model with the same dimensions. An existing graph without
that identity is rejected in Vertex mode; use a fresh graph or `--reset`. Use a dedicated graph/output pair for
each configuration; this local identity file does not describe arbitrary other databases.

HTTP accounting includes SDK retries, prompt/candidate/thinking tokens, embedding token statistics, and separate
reranker calls. This is usage accounting, not a currency estimate. Gemini uses stock Graphiti's numeric relevance
reranker, which avoids OpenAI token IDs and logprobs. Its three-token answer budget is still a real-model test risk,
especially with thinking models; inspect the reranker results before treating them as a baseline.

`--fake` and `--profile` cannot be combined with `--vertex`. Offline Vertex tests use the real SDK and Graphiti
clients over a simulated HTTP transport, with no cloud calls. They verify request schemas and accounting, not
extraction quality. The Spark profile below remains available until the Vertex cutover is validated.

Provider references: [Google Gen AI SDK](https://googleapis.github.io/python-genai/),
[retrieval task types](https://cloud.google.com/vertex-ai/generative-ai/docs/embeddings/task-types),
[Graphiti Gemini clients](https://github.com/getzep/graphiti/tree/v0.31.2/graphiti_core/llm_client).

## The real run on the Spark profile (legacy fallback)

From the repo root, on a machine that can reach the Spark, with Python 3.12 and Docker. A throwaway Neo4j, on the version the cluster runs:

    docker run -d --name graphiti-test-neo4j -p 127.0.0.1:7687:7687 \
      -e NEO4J_AUTH=neo4j/change-me-locally -e NEO4J_ACCEPT_LICENSE_AGREEMENT=yes neo4j:2026.09.0-community
    cd services/pipeline
    python3.12 -m venv ../../data/graphiti-venv
    ../../data/graphiti-venv/bin/pip install -r graphiti_test/requirements.txt

    ssh -f -N -L 8001:127.0.0.1:8001 shared-dev     # the chat model, as in services/pipeline/README.md
    export NEO4J_PASSWORD=change-me-locally
    export LLM_API_KEY=<from the Spark's ~/ember/.env> EMBEDDING_API_KEY=<same>
    export EMBEDDING_BASE_URL=http://<the Spark's public address>:8000/v1

Then a sample first. Read `summary.md`, especially structured-output failures, before going on:

    ../../data/graphiti-venv/bin/python -m graphiti_test run --tenant photon --limit 40 \
      --profile local-spark --out ../../data/out/graphiti-spark-sample

And the reference, on a fresh graph:

    ../../data/graphiti-venv/bin/python -m graphiti_test run --tenant photon --reset \
      --profile local-spark --out ../../data/out/graphiti-spark

Do not grow `--limit` on an existing graph for a reference: older rows would then be applied after newer ones. The run warns and counts them as `out_of_order`. Everything under `data/` is ignored by git. A stopped run resumes with the same command without `--reset`. Remove the container afterwards with `docker rm -f graphiti-test-neo4j`.

## Tests

From `services/pipeline`, with `graphiti_test/requirements.txt` installed:

    python -m unittest discover -s graphiti_test -t . -p "test_*.py" -v

- `test_harness.py`: rows, sampling, episodes, ledger and resume, counters, reference and diff, fake models, boundaries.
- `test_graphiti.py`: stock clients with the fake, the ontology against Graphiti's validation, the cross-encoder and null-attribute failures.
- `test_neo4j_integration.py`: real Graphiti and Neo4j with the fake. Skipped unless `NEO4J_TEST_URI` is set (with `NEO4J_PASSWORD`). Use a throwaway database.

CI's `pipeline` job runs them in their own venv (step `Graphiti test harness`), so `graphiti-core` stays out of `services/pipeline/requirements.txt`. The loader, schema and migrations are unchanged.

## Decisions along the way

- **A new fake instead of `models/fake_server.py`.** That server answers every chat "pong" and returns embeddings out of order on purpose. Graphiti needs replies per response model, which this one keys on the schema name.
- **`OpenAIGenericClient` for the Spark.** It is Graphiti's client for OpenAI-compatible servers, and uses constrained decoding through `json_schema`.
- **Temperature 0**, where stock `LLMConfig` defaults to 1, because a reference has to repeat.
- **No communities.** Ember does not build them (research Part 2), so the search recipes drop that scope.
- **Ties settled by natural identifier** in the reference. Without it, identical runs differ wherever two results score the same, because the order then follows random uuids. This removes most of the noise, not all, which is why `diff` reports result order apart.

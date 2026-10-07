# ADR 0004: Cloud LLM and embedding for the development phase

- Status: Accepted
- Date: 2026-10-06
- Jira: EMBER-49

## Context

The pipeline needs a language model (later: extraction of decisions and cross-source links) and an
embedding model (retrieval). ADR 0001 chose a self-hosted embedding model on the shared DGX Spark.
That service is live, but we want to develop and benchmark against hosted models first, and we
expect to compare several models later (including Claude) against other tools.

Right now Ember is in development. We use test data and our own data only. When the UI exists, a
customer will connect their sources through OAuth, which is when their consent covers sending data
to a provider.

## Decision

Use OpenAI for both the chat model and the embedding model during development, with the cheapest
suitable models:

- Chat: `gpt-5-nano` at $0.05 per million input tokens and $0.40 per million output tokens
- Embedding: `text-embedding-3-small` at $0.02 per million tokens, requested at 1024 dimensions
  (its default is 1536; the `dimensions` parameter shortens it)
- Prices checked on 2026-10-06, on OpenAI's pricing and model pages.

`gpt-5-nano` is a reasoning model, so some of the output tokens it bills are reasoning tokens that
do not appear in the reply. The next step up, `gpt-6-luna` ($0.10 input, $0.50 output per million
tokens), is the fallback if `gpt-5-nano` is not reliable enough on the extraction work.

Reasons, and what was not chosen:

- One provider, one key, one billing relationship. Gemini would also need only one key but is
  less familiar to the team; Claude has no embedding model of its own (Anthropic's documentation
  points to a separate provider, Voyage AI), so it would need a second provider.
- Graphiti (ADR 0002) defaults to OpenAI for both, which keeps the later semantic layer simple.
- The API format is the same one the Spark embedding service already serves, so one thin client
  covers OpenAI and the Spark, and any other provider that offers an OpenAI-compatible endpoint.
  Claude needs a small adapter (a later ticket) and stays a configuration change once it exists.
- The cheapest models keep development cost low. Scores from them are not comparable with scores
  from stronger models (EMBER-58 labels every result with the model used).

Models are chosen by profile in `config/models.toml`. Switching is a change to `MODEL_PROFILE`.
API keys are read from environment variables and are never in the repository.

## Consequences

- This supersedes ADR 0001's default for the development phase only. The Spark service and its
  profile (`local-spark`) stay available. Each graph still holds vectors from exactly one model, so
  switching profiles on a graph that already has embeddings requires a full re-embed (ADR 0001);
  the pipeline refuses to mix them.
- The embedding identity recorded for a cloud profile uses the provider's model id, the revision
  `provider-managed` (providers can change a model behind the same id; this is a known weakness),
  1024 dimensions, and no query prefix.
- Before any customer data is sent, review the provider's data-retention and training terms, and
  keep the self-hosted path working as the fallback for customers who need it.
- As of 2026-10-06, OpenAI states that API data is not used to train its models unless we opt in,
  and that abuse-monitoring logs are kept for up to 30 days unless the law requires longer. Zero
  Data Retention is available only to approved customers.
- A leaked key costs money. Keys live in `.env` (gitignored) or the deployment's secret store,
  and a spending limit should be set on the provider account.

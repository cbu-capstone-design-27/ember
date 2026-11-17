-- EMBER-77: one embedding per source_records row (ADR 0001: qwen3-embedding-0.6b, 1024 dimensions),
-- the exact text that was embedded, and the indexes the embed worker claims and EMBER-78 searches by.
-- The doc's Storage SQL is the source of truth for these. vector is not a trusted extension, so
-- creating it needs a superuser; where it already exists, "if not exists" skips that check.

-- migrate:up
create extension if not exists vector;

alter table source_records
  add column embedding   vector(1024),   -- ADR 0001's model; null until the embed worker runs
  add column embed_text  text,           -- exactly what was embedded, so a model switch re-embeds without re-parsing
  add column embedded_at timestamptz;

create index source_records_unembedded on source_records (id) where embedded_at is null;
create index source_records_embedding on source_records using hnsw (embedding vector_cosine_ops);

-- migrate:down
drop index source_records_embedding;
drop index source_records_unembedded;
alter table source_records drop column embedded_at, drop column embed_text, drop column embedding;
drop extension vector;

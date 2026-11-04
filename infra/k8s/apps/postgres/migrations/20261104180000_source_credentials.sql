-- EMBER-147: per-account source credentials (ADR 0010, "Credentials"). The doc's Storage SQL is the
-- source of truth. The application encrypts every row with AES-256-GCM before it gets here, using the
-- key ring for its kind (packages/source-credentials), so Postgres never sees a token.

-- migrate:up
create table source_credentials (
  source     text not null,
  account_id text not null,
  kind       text not null check (kind in ('api', 'webhook')),   -- api: enrich calls the source with it; webhook: intake verifies with it
  key_id     text not null,          -- which key of that kind's ring sealed it, so a rotation re-encrypts by key
  ciphertext bytea not null,         -- 12-byte nonce, ciphertext, 16-byte tag; the additional data names (source, account_id, kind)
  updated_at timestamptz not null default now(),
  primary key (source, account_id, kind),
  foreign key (source, account_id) references installations (source, account_id) on delete cascade
);

-- migrate:down
drop table source_credentials;

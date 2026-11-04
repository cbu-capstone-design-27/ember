# source-credentials

Seals and opens rows of ingestion's `source_credentials` table (EMBER-147, [ADR 0010](../../docs/adr/0010-connecting-sources.md#credentials)). These are the per-account credentials an owner connects a source with: a Jira OAuth token pair, a Slack bot token, a GitLab access token or webhook signing token.

**Consumers:**

- The web app seals rows when an owner connects a source, with the TypeScript twin [`apps/web/src/lib/source-credentials.ts`](../../apps/web/src/lib/source-credentials.ts).
- Python workers open them: enrich reads `api` rows (`enrich:jira` from EMBER-131, `enrich:slack` from EMBER-148, `enrich:gitlab` from EMBER-149), and intake reads `webhook` rows (GitLab, EMBER-149).

Each Python consumer copies `source_credentials.py` into its image and adds the `cryptography` pin from `requirements.txt` to its own. Each maps the table with its own SQLAlchemy model, as `AGENTS.md` asks.

## Format

| Part | Value |
|---|---|
| Cipher | AES-256-GCM, 12-byte random nonce, 16-byte tag |
| `ciphertext` column | nonce, then ciphertext, then tag |
| Additional data | `ember:source_credentials:v1`, source, account id and kind, NUL-separated. A row copied to another account, source or kind fails to open |
| Plaintext | A JSON object, e.g. `{"access_token": "…", "refresh_token": "…"}` |
| `key_id` column | Which key in the ring sealed the row |

## Key rings

There is one ring per kind, so ADR 0004's rule holds: the credential that verifies deliveries and the one that calls the source never sit in the same pod.

| Kind | Variable | Given to |
|---|---|---|
| `api` | `SOURCE_CREDENTIALS_API_KEYS` | web app, enrich workers |
| `webhook` | `SOURCE_CREDENTIALS_WEBHOOK_KEYS` | web app, intake |

The value is `<key id>:<base64 of 32 bytes>`, comma-separated. The first key seals and every key opens. A key id is 1 to 32 of `A-Z a-z 0-9 _ -`, e.g. a month such as `2026-11`. Make a key with `openssl rand -base64 32`.

**Rotating:**

1. Put the new key first and keep the old one after it, everywhere that ring is used.
2. Re-seal every row whose `key_id` is the old one.
3. Drop the old key.

A row whose key is no longer in the ring fails to open, and says so.

## Test

`fixtures/vectors.json` holds test-only keys and the rows they seal. Both implementations must open every case and, given its nonce, seal it to the same bytes.

```sh
pip install -r packages/source-credentials/requirements.txt
python3 packages/source-credentials/test_source_credentials.py
cd apps/web && npm test        # test/connections.test.ts reads the same vectors
```

# packages/ingestion-envelope

Shared v1 intake every ingestion source emits into the core pipeline.

Jira: EMBER-39. Shape is `{type, body}`: a source name and opaque raw connector JSON. The processing pipeline cleans and sorts later. Embeddings stay out (EMBER-3).

## Consumers

- `services/pipeline` and each source connector (GitHub, Jira, Slack, Teams, GitLab) emit this shape.
- `services/intake` wraps every webhook delivery as `{"type":"<source>","body":<raw object>}` in `raw_events.envelope` (EMBER-62). `services/github-ingestion`'s backfill wraps each REST object as `{"type":"github","body":<raw object>}` (EMBER-35).
- `services/backend` (intake and `backfill:slack`) stores a Slack Events API delivery as `{"type":"slack","body":<raw object>}`; the backfill shapes each message as its webhook delivery (EMBER-36, EMBER-160).
- `services/backend` (intake and `backfill:jira`) stores `{"type":"jira","body":<raw object>}`; the backfill shapes each issue and comment as its webhook delivery (EMBER-31, EMBER-167).
- `services/backend` (intake and `backfill:gitlab`) stores a GitLab webhook, or a backfilled REST object, as `{"type":"gitlab","body":<raw object>}` (EMBER-54, EMBER-160).
- Downstream graph mapping (EMBER-3) reads it. It does not own the fields.

The package lands before those consumers have code because the connectors are separate spikes and need one contract to aim at.

## Layout

- `schema/envelope.v1.schema.json` — `{type, body}` intake. `body` is any object.
- `fixtures/` — one golden raw sample per source.
- `validate.py` — stdlib checker for the schema subset this contract uses.
- `test_contract.py` — validates each fixture.

Contract write-up: `docs/contracts/ingestion-payload.md`.

```sh
python3 packages/ingestion-envelope/test_contract.py
```

# packages/ingestion-envelope

Shared v1 intake every ingestion source emits into the core pipeline.

Jira: EMBER-39. Shape is `{type, body}`: a source name and opaque raw connector JSON. The processing pipeline cleans and sorts later. Embeddings stay out (EMBER-3).

## Consumers

- `services/pipeline` and each source connector (GitHub, Jira, Slack, Teams, GitLab) emit this shape.
- `services/github-ingestion` is the first emitter (EMBER-35). It wraps a GitHub webhook as `{"type":"github","body":<raw object>}`.
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

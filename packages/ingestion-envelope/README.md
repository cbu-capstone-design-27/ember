# packages/ingestion-envelope

Shared v1 envelope every ingestion source emits into the core pipeline.

Jira: EMBER-39. Embeddings stay out of this envelope (EMBER-3).

## Consumers

- `services/pipeline` and each source connector (GitHub, Jira, Slack, Teams, GitLab) emit this shape.
- Downstream graph mapping (EMBER-3) reads it. It does not own the fields.

The package lands before those consumers have code because the connectors are separate spikes and need one contract to aim at.

## Layout

- `schema/envelope.v1.schema.json` — one envelope. Source-specific fields live in `payload`.
- `fixtures/` — one golden sample per source.
- `validate.py` — stdlib checker for the schema subset this contract uses.
- `test_contract.py` — validates each fixture.

Contract write-up: `docs/contracts/ingestion-payload.md`.

```sh
python3 packages/ingestion-envelope/test_contract.py
```

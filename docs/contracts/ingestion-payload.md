# Ingestion payload contract (v1)

Jira: EMBER-39. Schema: `packages/ingestion-envelope/schema/envelope.v1.schema.json`.

This contract is the shared intake schema for all raw connector data. Ingestion workers emit into `{type, body}`. The processing pipeline (not this schema) cleans and sorts.

Embeddings stay out (EMBER-3). This object has no embedding field.

```json
{
  "type": "<Ingestion Source>",
  "body": {}
}
```

| Field | Required | Contents |
| --- | --- | --- |
| `type` | yes | `github`, `jira`, `slack`, `teams`, or `gitlab` |
| `body` | yes | Opaque raw connector JSON. Any object |

`body` is the intake dump: a webhook or API payload as the connector received it. This contract does not require a typed per-source shape, and it does not require identity, source, time, content refs, metadata, or `raw_ref` as first-class fields.

## Fixtures

One sample each, under `packages/ingestion-envelope/fixtures/`:

- `github.json` — GitHub `issues` `opened` webhook. `services/github-ingestion` emits this shape from `POST /webhook/github` (EMBER-35).
- `jira.json` — Jira `jira:issue_updated` webhook
- `slack.json` — Slack `event_callback` message
- `teams.json` — Microsoft Graph change notification
- `gitlab.json` — GitLab merge request hook

```sh
python3 packages/ingestion-envelope/test_contract.py
```

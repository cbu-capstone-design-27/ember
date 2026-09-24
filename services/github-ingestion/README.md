# services/github-ingestion

GitHub webhook connector. Each delivery is emitted as EMBER-39 intake: `{"type":"github","body":<raw JSON object>}`.

Jira: EMBER-35. Floor scaffold. This service does not clean or sort the payload. Embeddings stay out (EMBER-3). No live tunnel and no webhook registration in this change.

## Status

Stub HTTP receiver. It checks an optional HMAC, wraps the raw body, and writes the envelope to stdout. No queue yet.

## Contract

`POST /webhook/github` with a JSON object body.

When `GITHUB_WEBHOOK_SECRET` is set, the receiver requires `X-Hub-Signature-256` (`sha256=` plus the hex HMAC-SHA256 of the raw body). When the secret is unset, verification is skipped and a warning is logged so a local stub can run.

The logged line is exactly:

```json
{"type":"github","body":{}}
```

`body` is the parsed webhook JSON, unchanged. Schema: `packages/ingestion-envelope`.

`GITHUB_REPO` (for example `ryan-stoffel/photon`) is logged at startup. The stub accepts every delivery; it does not filter on that repository yet.

`GET /health` returns `{"status":"ok"}`.

## Run

```sh
python3 services/github-ingestion/receiver.py
```

`GITHUB_INGESTION_PORT` defaults to `8080`.

## Test

```sh
python3 services/github-ingestion/test_webhook.py
```

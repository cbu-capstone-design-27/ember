# services/github-ingestion

GitHub App webhook connector. Each delivery is emitted as EMBER-39 intake: `{"type":"github","body":<raw JSON object>}`.

Jira: EMBER-35. Floor scaffold. The worker is repo- and account-agnostic. It does not clean or sort the payload. Embeddings stay out (EMBER-3). No live tunnel and no App registration in this change.

Installing the App on an account or org defaults to all repositories. Choosing which of those repositories feed Ember is a later webapp concern, not this service.

## Status

Stub HTTP receiver. It checks an optional HMAC, wraps the raw body, and writes the envelope to stdout. No queue yet. It does not load `GITHUB_APP_ID` or the private key, and it does not call the GitHub API.

## Contract

`POST /webhook/github` with a JSON object body.

When `GITHUB_WEBHOOK_SECRET` is set, the receiver requires `X-Hub-Signature-256` (`sha256=` plus the hex HMAC-SHA256 of the raw body). When the secret is unset, verification is skipped and a warning is logged so a local stub can run.

The logged line is exactly:

```json
{"type":"github","body":{}}
```

`body` is the parsed webhook JSON, unchanged. Schema: `packages/ingestion-envelope`.

When the payload includes them, the process log also records `installation.id` and `repository.full_name` for later routing. Missing fields are logged as empty. The receiver never filters on a configured repository.

`GET /health` returns `{"status":"ok"}`.

## GitHub App placeholders

This is a GitHub App webhook endpoint, not a classic OAuth app and not a single-repo webhook. The lists below are what the App will need when it is created. Nothing here is requested or subscribed yet.

### Credentials

| Variable | Role |
| --- | --- |
| `GITHUB_APP_ID` | App id. Placeholder; not read by the stub. |
| `GITHUB_APP_PRIVATE_KEY` | PEM path, or the PEM contents including the BEGIN/END lines. Placeholder; not loaded by the stub. A path is the compose-friendly option. |
| `GITHUB_WEBHOOK_SECRET` | Used now for HMAC. Empty skips verification. |

### Permissions (placeholder)

Repository permissions, read only, to request when the App is created:

- Metadata
- Contents
- Issues
- Pull requests

No organization permissions in this scaffold.

### Events (placeholder)

- `installation`
- `installation_repositories`
- `repository`
- `push`
- `issues`
- `issue_comment`
- `pull_request`
- `pull_request_review`
- `pull_request_review_comment`

The stub accepts any JSON object. It does not filter on event name.

## Run

```sh
python3 services/github-ingestion/receiver.py
```

`GITHUB_INGESTION_PORT` defaults to `8080`.

Own container, optional compose profile. A plain `up` still starts only Neo4j:

```sh
docker compose -f infra/docker-compose.yml --env-file .env \
  --profile github-ingestion up --build github-ingestion
```

## Test

```sh
python3 services/github-ingestion/test_webhook.py
```

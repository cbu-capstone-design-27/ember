# services/intake

The one listener for every source's webhooks (EMBER-62). It verifies each delivery, stores it as it arrived in Postgres `raw_events`, and answers. It never calls a source API: the per-source enrich workers fetch what a payload lacks. The design is [`docs/ingestion/architecture.md`, "The parent listener (intake)"](../../docs/ingestion/architecture.md#the-parent-listener-intake).

GitHub, Slack and Jira have adapters so far. Adding a source means adding an adapter under `adapters/` and listing it in `intake.py`'s `main`, not a new listener.

## Contract

| Request | Answer |
| --- | --- |
| `POST /webhook/{source}`, stored | 200 `{"status":"stored"}` |
| The same `(source, delivery_id)` again | 200 `{"status":"duplicate"}`, nothing written |
| Signature check failed | 401 |
| Body is not a JSON object, a required header is missing, or Postgres can't store the payload (`\u0000`, a lone surrogate) | 400 |
| Unknown `{source}` | 404 |
| Body over 8 MiB, the same cap as nginx's `client_max_body_size` | 413 |
| Postgres unreachable, restarting, or too slow to commit within 8 s; nothing stored | 503 |
| Anything else (a bug) | 500, counted as `error` |
| `GET /health` | 200 when Postgres answers, else 503. The readiness probe |
| `GET /metrics` | Prometheus text: `intake_deliveries_total{source,result}` |

Each stored row has `origin = 'webhook'`, the envelope `{"type": "<source>", "body": <payload>}` (EMBER-39), and `tenant` from `installations` for `(source, account_id)`, or null until the account is mapped. Inserts use `on conflict (source, delivery_id) do nothing`. The `raw_events` trigger (EMBER-75) wakes the enrich workers on each insert, so a duplicate wakes no one.

`result` in the metric is `stored`, `duplicate`, `handshake`, `unauthorized`, `bad_request`, `too_large`, `disconnected`, `unstorable`, `unavailable`, `error` or `unknown_source`. An unknown source is counted as `source="unknown"`, so made-up paths can't add series. Logs name the delivery, the result and an error's type, never the payload.

At most 8 deliveries are read and handled at once; the rest wait unread. Parsing and storing a delivery takes up to about 15 times its size, so 8 near the 8 MiB cap peak around 800 MiB, which the cluster's 1 GiB limit covers. A typical delivery is tens of KiB.

SIGINT or SIGTERM stops accepting connections and lets requests in flight finish (up to 20 s), then the process exits.

## Configuration

| Variable | Role |
| --- | --- |
| `DATABASE_URL` | Postgres, e.g. `postgresql://ember:...@localhost:5432/ember?sslmode=disable`. On the cluster, Secret `postgres-app`, key `uri` |
| `GITHUB_WEBHOOK_SECRET` | The GitHub App's webhook secret. Required: intake refuses to start without it, so it never stores an unsigned delivery. It also refuses one with leading or trailing whitespace, which no signature would match |
| `SLACK_SIGNING_SECRET` | The Slack app's signing secret (Basic Information, App Credentials). Optional: without it intake serves GitHub only and `/webhook/slack` is an unknown source (404), so intake runs where the Slack app isn't set up. When set, the same whitespace rule as GitHub's applies. The startup log names the sources served |
| `JIRA_WEBHOOK_SECRET` | The secret saved on the Jira site's admin webhook. Optional, like Slack's: without it `/webhook/jira` is an unknown source (404). When set, the same whitespace rule applies |
| `LISTEN_PORT` | Defaults to `8080`. Not `INTAKE_PORT`, which Kubernetes sets for a Service called `intake` |

## GitHub adapter

[`docs/ingestion/github-webhooks.md`](../../docs/ingestion/github-webhooks.md) is the spec.

- **Verification:** `X-Hub-Signature-256`, the HMAC-SHA256 of the raw body with the App's webhook secret.
- **`delivery_id`:** `X-GitHub-Delivery`, at most 255 characters. A redelivery reuses it.
- **`account_id`:** `installation.id`.
- **`event_name`:** `X-GitHub-Event`, plus `:<action>` when there is one, e.g. `pull_request:synchronize`.
- **`occurred_at`:** the [ordering value](../../docs/ingestion/github-webhooks.md#ordering-value-ember-63): the object's `updated_at`, a submitted review's `submitted_at`, a published release's `published_at` then `created_at`, or a push's `repository.pushed_at`. Deletes, dismissals, review and release edits, and every other event get `received_at`, as does a time that is missing or unreadable.

### Events

Subscribe the App to these. Intake does not filter on them, and with the App's current permissions GitHub only sends some of them ([`github-webhooks.md`, "Event types"](../../docs/ingestion/github-webhooks.md#event-types-payload-and-what-enrich-fetches)). The list is `RECOMMENDED_WEBHOOK_EVENTS` in `adapters/github.py`; `test_intake.py` fails if the two drift.

`installation`, `installation_repositories`, `repository`, `push`, `create`, `delete`, `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`, `commit_comment`, `release`, `milestone`, `label`, `status`, `check_run`, `check_suite`, `workflow_run`, `workflow_job`, `deployment`, `deployment_status`, `discussion`, `discussion_comment`

## Slack adapter

[`docs/ingestion/slack-webhooks.md`](../../docs/ingestion/slack-webhooks.md) is the spec. `enrich:slack` (`services/slack-ingestion/enrich.py`) reads the rows.

- **Verification:** `X-Slack-Signature`, `v0=` and the HMAC-SHA256 of `v0:<X-Slack-Request-Timestamp>:<raw body>` with the signing secret. A timestamp more than 5 minutes from now is refused, so a captured request can't be replayed.
- **Handshake:** the Request URL check (`url_verification`) is signed too. Intake verifies it, answers `{"challenge": …}` and stores nothing.
- **`delivery_id`:** `event_id`, which Slack repeats on its retries (`X-Slack-Retry-Num`, logged). The `app_rate_limited` notice has none, so it is `rate_limited:<team_id>:<minute_rate_limited>`. Any other body is a 400.
- **`account_id`:** `team_id`, the workspace.
- **`event_name`:** the inner `event.type`, plus `:<subtype>` when there is one, e.g. `message:message_deleted`; `app_rate_limited` for the notice.
- **`occurred_at`:** the [ordering value](../../docs/ingestion/slack-webhooks.md#ordering-value-ember-63): `event.event_ts`, then `event.ts`, then the wrapper's `event_time`, to the microsecond; `minute_rate_limited` for the notice. One that is unreadable gets `received_at`.

The app's scopes and events are in `services/slack-ingestion/slack-app-manifest.json`. Set its Event Subscriptions Request URL to `https://ember.tail470a31.ts.net/webhook/slack` once intake has the secret: Slack sends the URL check straight away.

## Jira adapter

[`docs/ingestion/jira-webhooks.md`](../../docs/ingestion/jira-webhooks.md) is the spec. `enrich:jira` (`services/jira-ingestion/enrich.py`) reads the rows.

- **Verification:** `X-Hub-Signature`, `sha256=` and the HMAC-SHA256 of the raw body with the admin webhook's secret (WebSub `method=signature`). Only `sha256` is accepted, and a webhook saved without a secret is refused.
- **`delivery_id`:** `<site>:<X-Atlassian-Webhook-Identifier>`, at most 255 characters. Jira makes the identifier unique only within a site and repeats it on its retries (`X-Atlassian-Webhook-Retry`, logged with `X-Atlassian-Webhook-Flow`).
- **`account_id`:** the site, the host of the first `self` URL among the event's own entity (`comment`, `worklog`, `attachment`, `issueLink`, …), then `issue`, then `user`, e.g. `ember-capstone.atlassian.net`. Admin bodies often omit `cloudId`. A body with no `self` URL is stored with no account and `delivery_id` `unknown:<identifier>`.
- **`event_name`:** `webhookEvent` as sent, e.g. `jira:issue_updated`, `comment_created`. A body without it is a 400.
- **`occurred_at`:** the [ordering value](../../docs/ingestion/jira-webhooks.md#ordering-value-ember-63): `issue.fields.updated`, `comment.updated` or `worklog.updated`, then `attachment.created` for `attachment_created`, then the body's `timestamp` (epoch ms). Deletes, links and properties use `timestamp`, since a deleted entity's `updated` is its last edit. One that is unreadable gets `received_at`.

Jira counts any answer but 200 as a failure and retries; intake answers 200 for stored and duplicate deliveries. Register the admin webhook (Jira Administration → System → Webhooks) with the URL `https://ember.tail470a31.ts.net/webhook/jira` and the same secret as `JIRA_WEBHOOK_SECRET`, once intake has it.

## Run

Against the local Postgres, from the repo root:

```sh
docker compose -f infra/docker-compose.yml --env-file .env --profile postgres up -d postgres
docker compose -f infra/docker-compose.yml --env-file .env --profile postgres run --rm postgres-migrate
docker compose -f infra/docker-compose.yml --env-file .env --profile intake up --build intake
```

Or without the container:

```sh
pip install -r services/intake/requirements.txt
DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
  GITHUB_WEBHOOK_SECRET=throwaway-local-secret SLACK_SIGNING_SECRET=throwaway-local-secret \
  python3 services/intake/intake.py
```

On the cluster it runs from `infra/k8s/apps/intake`, behind nginx at `https://ember.tail470a31.ts.net/webhook/{source}`. [`infra/k8s/README.md`, "intake"](../../infra/k8s/README.md#intake) covers the Deployment, its logs and metrics, and setting the webhook secret.

To send real deliveries to a local intake instead, point a tunnel at the port (`cloudflared tunnel --url http://127.0.0.1:8080`) and set the App's webhook URL to `https://<tunnel-host>/webhook/github` with the same secret, or the Slack app's Request URL to `https://<tunnel-host>/webhook/slack` with its signing secret in `SLACK_SIGNING_SECRET`.

## Test

`test_intake.py` runs intake against a real Postgres, both in process and as `python3 intake.py`. It needs psql, dbmate and the packages in `requirements.txt`, like `infra/k8s/apps/postgres/test_migrations.py`:

```sh
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
  python3 services/intake/test_intake.py
```

CI runs it in the `sanity` job on the `pgvector/pgvector:0.8.7-pg18-trixie` container.

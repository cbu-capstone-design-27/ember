# services/intake

The one listener for every source's webhooks (EMBER-62). It verifies each delivery, stores it as it arrived in Postgres `raw_events`, and answers. It never calls a source API: the per-source enrich workers fetch what a payload lacks. The design is [`docs/ingestion/architecture.md`, "The parent listener (intake)"](../../docs/ingestion/architecture.md#the-parent-listener-intake).

GitHub is the only adapter so far. Adding a source means adding an adapter under `adapters/` and listing it in `intake.py`'s `main`, not a new listener.

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
  GITHUB_WEBHOOK_SECRET=throwaway-local-secret python3 services/intake/intake.py
```

On the cluster it runs from `infra/k8s/apps/intake`, behind nginx at `https://ember.tail470a31.ts.net/webhook/{source}`. [`infra/k8s/README.md`, "intake"](../../infra/k8s/README.md#intake) covers the Deployment, its logs and metrics, and setting the webhook secret.

To send real deliveries to a local intake instead, point a tunnel at the port (`cloudflared tunnel --url http://127.0.0.1:8080`) and set the App's webhook URL to `https://<tunnel-host>/webhook/github` with the same secret.

## Test

`test_intake.py` runs intake against a real Postgres, both in process and as `python3 intake.py`. It needs psql, dbmate and the packages in `requirements.txt`, like `infra/k8s/apps/postgres/test_migrations.py`:

```sh
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
  python3 services/intake/test_intake.py
```

CI runs it in the `sanity` job on the `pgvector/pgvector:0.8.7-pg18-trixie` container.

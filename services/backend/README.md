# services/backend

`ember-backend` is Ember's Python backend in one image and one process (EMBER-160, [ADR 0012](../../docs/adr/0012-one-backend-image.md)). It holds intake (the webhook listener), `enrich:github`, `enrich:jira`, Jira's keep-alive and `enrich:slack` today. GitLab's enrich worker and embed move in one connector at a time, and each old service is deleted once this image does its job.

The design of each stage is unchanged: [`docs/ingestion/architecture.md`](../../docs/ingestion/architecture.md). What changes is how the stages are packaged: one process runs every component, and one bad source can't take down the rest.

## How it runs

```
python -m ember run
  └─ Controller   loads settings, builds each connector's parts, handles SIGTERM and SIGINT
       └─ Supervisor   one task per component; restarts a crashed one, leaves the others alone
            ├─ http           /webhook/{source}, /health, /status, /metrics on LISTEN_PORT
            ├─ intake         stores every verified delivery in raw_events
            ├─ loop           watches event-loop lag
            ├─ enrich:github  raw_events -> source_records (connectors/github/enrich.py)
            ├─ enrich:jira    raw_events -> source_records (connectors/jira/enrich.py)
            ├─ keep-alive:jira  daily at 10:17 UTC: refresh Connect Jira's tokens, extend its webhooks
            ├─ enrich:slack   raw_events -> source_records (connectors/slack/enrich.py)
            └─ enrich:<source>, …   as the other connectors move in
```

- **Component** (`runtime/component.py`): what the supervisor runs. `start()` acquires what it needs, `run(stopping)` works until shutdown, `close()` releases. Each component makes its own Postgres engine, so one exhausting its pool or losing its connections doesn't starve another.
- **Connector** (`connectors/base.py`): one per source. It loads that source's settings and builds its parts: the webhook adapter intake serves, its enrich worker, and its scheduled jobs (Jira's keep-alive). Each part is built on its own, so a source with a webhook secret but no API credentials still receives deliveries.
- **QueueWorker** (`work/queue_worker.py`): the base of every worker that claims rows from Postgres. It wakes on `LISTEN/NOTIFY`, catches up on start, claims with `for update skip locked` in the source's order (EMBER-63), runs up to `concurrency` jobs at once, and sets failed rows aside by failure class (`core/errors.py`).
- **Deps** (`runtime/deps.py`): the shared dependencies, built once and passed to constructors: the database, the credential key rings, the metrics registry. No DI framework.
- **Shared source plumbing**: `core/http.py` (retries, rate-limit headers, failure classes), `work/rate_limits.py` (the `rate_limits` row every replica waits on) and `core/credentials.py` (a connected account's sealed token, used only for its own tenant's rows; disconnecting an account whose token the source refused). A connector adds only its own rules: `connectors/slack/enrich.py` is what each Slack event becomes and three lookups.

### When something fails

| What | What happens |
| --- | --- |
| `DATABASE_URL` missing or invalid | The process exits 2 |
| A source's secret missing or unusable (`GITHUB_WEBHOOK_SECRET` with a trailing newline, a GitLab token that isn't `whsec_`, an unreadable key ring) | That part is disabled and `/status` says why. `/webhook/<source>` answers 404. Everything else runs |
| A component crashes (an exception, or `run` returns before shutdown) | Restarted after 1 s, 2 s, 4 s … up to `EMBER_RESTART_MAX` (60 s), forever. The delay starts again from 1 s once it has run 5 minutes. Nothing else restarts |
| A component raises `ConfigError` on start | Disabled for the life of the process, with the reason |
| A worker's settings unusable (`GITHUB_APP_PRIVATE_KEY_PATH` unreadable, a key that isn't PEM, `SOURCE_CREDENTIALS_API_KEYS` unreadable) | `workers:<source>` is disabled with the reason. Intake still stores that source's deliveries, which wait in `raw_events` |
| A row fails in a worker | Only that row is set aside, by class: a rate limit until the source allows the next call, a 5xx or network error with backoff (2 s doubling to 5 minutes), a missing object or a permission error for an hour. The rest keep going. The skip list is in memory, so a restart retries every row once |
| `keep-alive:jira` fails for a site (Atlassian down, a webhook id it no longer knows) | Logged, and counted in `/status`'s `last_run.failed`. The next day's run tries again, well inside the webhooks' 30 days. A refresh refused with `invalid_grant` disconnects the site instead, so Settings asks for a reconnect |
| Postgres down | `/health` answers 503, intake answers 503 (the source retries), workers crash and retry until it is back |

### Shutdown

On SIGTERM or SIGINT, `/health` answers 503 and the HTTP server stops accepting and finishes the requests in hand (up to 20 s). Then every other component sees the stop: workers claim nothing more and finish the jobs in hand. All of it within `EMBER_SHUTDOWN_GRACE` (50 s, inside Kubernetes' 60); a component still running then is cancelled. A second signal cancels at once. The process exits 0.

## HTTP

| Request | Answer |
| --- | --- |
| `POST /webhook/{source}` | Intake's contract, below |
| `GET /health` | 200 when Postgres answers, 503 when it doesn't or once shutdown starts. The readiness probe |
| `GET /status` | JSON: each component's state (`starting`, `running`, `backoff`, `disabled`, `stopped`), restarts and last error, and why each disabled part is disabled. No secret or payload, but keep it off the internet: nginx exposes only `/webhook/` |
| `GET /metrics` | Prometheus text: `intake_deliveries_total{source,result}`, `ember_component_up{component}`, `ember_component_restarts_total{component}`, `ember_event_loop_lag_seconds` |

### Intake

The same contract as `services/intake` (EMBER-62), which this replaces:

| Request | Answer |
| --- | --- |
| `POST /webhook/{source}`, stored | 200 `{"status":"stored"}` |
| The same `(source, delivery_id)` again | 200 `{"status":"duplicate"}`, nothing written |
| Signature check failed | 401 |
| Body is not a JSON object, a required header is missing, or Postgres can't store the payload (`\u0000`, a lone surrogate) | 400 |
| Unknown or disabled `{source}` | 404 |
| Body over 8 MiB, the same cap as nginx's `client_max_body_size` | 413 |
| Postgres unreachable, restarting, or too slow to commit within 8 s; nothing stored | 503 |
| Anything else (a bug) | 500, counted as `error` |

Each stored row has `origin = 'webhook'`, the envelope `{"type": "<source>", "body": <payload>}` (EMBER-39), `occurred_at` from the adapter (EMBER-63), and `tenant` from `installations` for a connected `(source, account_id)`, or null. The `raw_events` trigger (EMBER-75) wakes the workers on each insert, so a duplicate wakes no one. Logs name the delivery, the result and an error's type, never the payload. At most 8 deliveries are read and handled at once.

How each source is verified, and where its `delivery_id`, `account_id`, `event_name` and `occurred_at` come from, is unchanged: [`services/intake/README.md`](../intake/README.md#github-adapter) until that service is removed, and `docs/ingestion/<source>-webhooks.md`. The adapters are in `connectors/<source>/webhook.py`.

Subscribe the GitHub App to these events (`RECOMMENDED_WEBHOOK_EVENTS` in `connectors/github/webhook.py`; `tests/test_intake.py` fails if the two drift):

`installation`, `installation_repositories`, `repository`, `push`, `create`, `delete`, `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`, `commit_comment`, `release`, `milestone`, `label`, `status`, `check_run`, `check_suite`, `workflow_run`, `workflow_job`, `deployment`, `deployment_status`, `discussion`, `discussion_comment`

## Configuration

Only `DATABASE_URL` is required. Everything else turns a part on.

| Variable | Role |
| --- | --- |
| `DATABASE_URL` | Postgres, e.g. `postgresql://ember:...@localhost:5432/ember?sslmode=disable`. On the cluster, Secret `postgres-app`, key `uri` |
| `LISTEN_PORT` | Default `8080` |
| `EMBER_COMPONENTS` | Comma-separated names or shell patterns of the components to run, default `*`. `intake` in one container and `enrich:*` in another runs the same image as two |
| `EMBER_SHUTDOWN_GRACE` | Seconds components get to finish after SIGTERM, default `50` |
| `EMBER_RESTART_MAX` | The longest wait between restarts of a crashed component, default `60` |
| `GITHUB_WEBHOOK_SECRET` | Serves `/webhook/github` |
| `SLACK_SIGNING_SECRET` | Serves `/webhook/slack` |
| `JIRA_WEBHOOK_SECRET`, `JIRA_OAUTH_CLIENT_SECRET` | Either serves `/webhook/jira`: admin webhooks and Connect Jira's dynamic webhooks |
| `SOURCE_CREDENTIALS_WEBHOOK_KEYS` | The webhook key ring (`packages/source-credentials`). Serves `/webhook/gitlab` with the signing tokens Connect GitLab stores |
| `GITLAB_WEBHOOK_SIGNING_TOKEN`, `GITLAB_WEBHOOK_SECRET_TOKEN` | Shared GitLab tokens for webhooks set up by hand. Either serves `/webhook/gitlab` |
| `GITHUB_APP_ID`, and `GITHUB_APP_PRIVATE_KEY_PATH` (a mounted `.pem`) or `GITHUB_APP_PRIVATE_KEY` (the PEM itself) | The App `enrich:github` calls the API as, for the two events that fetch (pull request files, the commits of a full push). Without them it still enriches every other event; those two are set aside until the App is configured |
| `GITHUB_ENRICH_CONCURRENCY`, `GITHUB_ENRICH_BATCH` | `enrich:github`'s jobs at once (default 4) and rows per job (default 25) |
| `SOURCE_CREDENTIALS_API_KEYS`, `JIRA_OAUTH_CLIENT_ID`, `JIRA_OAUTH_CLIENT_SECRET` | The api key ring opens the token Connect Jira stored for each site; the Atlassian 3LO app refreshes it. `enrich:jira` needs them for the events that fetch an issue it hasn't stored (comments, worklogs, links); without them it still enriches every other event. `keep-alive:jira` is disabled without them |
| `JIRA_ENRICH_CONCURRENCY`, `JIRA_ENRICH_BATCH` | `enrich:jira`'s jobs at once (default 4) and rows per job (default 10) |
| `SOURCE_CREDENTIALS_API_KEYS` (again) | `enrich:slack` opens the bot token Connect Slack stored for each workspace with it, and doesn't start without it. A workspace's rows wait, unclaimed, until it is connected to their tenant |
| `SLACK_ENRICH_CONCURRENCY`, `SLACK_ENRICH_BATCH` | `enrich:slack`'s jobs at once (default 4) and rows per job (default 10) |

Secrets with leading or trailing whitespace are refused, since no signature would match them. Messages name the variable, never its value.

`python -m ember check` prints what `run` would start and why each other part is disabled, and connects to nothing.

## Run

With Docker, Postgres and its migrations included, from the repo root:

```sh
cp .env.example .env   # set GITHUB_WEBHOOK_SECRET and the other secrets you have
docker compose -f infra/docker-compose.yml --env-file .env --profile postgres --profile backend up -d --build
curl -s localhost:8080/status
```

Without Docker:

```sh
pip install -e services/backend
PYTHONPATH=packages/source-credentials \
DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
  GITHUB_WEBHOOK_SECRET=throwaway-local-secret python -m ember run
```

## Migrations

`migrations/` holds the schema as dbmate migrations ([`docs/ingestion/architecture.md`, Storage](../../docs/ingestion/architecture.md#storage) is the source of truth). The image ships them in `/opt/ember/migrations` with the pinned `dbmate`, and `DBMATE_MIGRATIONS_DIR` points at them, so the cluster's migrate Job runs the same image and tag as the backend with `dbmate up` as its command: schema and code always deploy together. Locally, the compose `postgres-migrate` service applies the same directory.

## Test

The tests run against a real Postgres. They need psql, dbmate and this package installed:

```sh
pip install services/backend
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 services/backend/tests/test_runtime.py
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 services/backend/tests/test_intake.py
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 services/backend/tests/test_enrich_github.py
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 services/backend/tests/test_enrich_jira.py
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 services/backend/tests/test_enrich_slack.py
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' python3 services/backend/tests/test_migrations.py
```

- `test_runtime.py`: the supervisor (restart, backoff, isolation, shutdown order), the queue worker (wake, catch-up, ordering, concurrency, replicas, failures, stop, reconnect), and the controller with a broken connector beside a working one.
- `test_intake.py`: intake's contract for every source, carried over from `services/intake`, and `python -m ember` as the container runs it.
- `test_enrich_github.py`: `enrich:github`'s records, fetches, ordering, bulk skip, rate limits and failure classes against a stubbed GitHub, carried over from `services/github-ingestion/test_enrich.py`, and how `GITHUB_APP_*` become the App's JWT.
- `test_enrich_slack.py`: `enrich:slack`'s records for messages (replies, edits, deletes), channels, people and membership, fetching only what isn't stored, waiting for a connected workspace, disconnecting on a refused token, an uninstall or our bot's revoked token (but not over a newer reconnect), and rate limits, against a stubbed Slack.
- `test_migrations.py`: the migrations apply, roll back, and build exactly the schema `docs/ingestion/architecture.md` defines.
- `test_enrich_jira.py`: `enrich:jira`'s records, issue fetches, token refresh, rate limits and failure classes, and `keep-alive:jira`'s refresh, disconnect, advisory lock and schedule, against a stubbed Atlassian. Carried over from `services/jira-ingestion/test_enrich.py` and `test_keep_alive.py`.

CI runs them in the `sanity` job on the `pgvector/pgvector:0.8.7-pg18-trixie` container, and builds the image.

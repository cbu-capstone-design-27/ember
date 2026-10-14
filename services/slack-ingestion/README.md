# services/slack-ingestion

Slack Events API connector. Each delivery is emitted as EMBER-39 intake: `{"type":"slack","body":<raw JSON object>}`.

Jira: EMBER-36. Parent epic: EMBER-7. Floor. The worker is workspace-agnostic. It does not clean, sort, filter, or reconstruct threads. That is the Slack source pipeline's job (EMBER-7). Embeddings stay out (EMBER-3).

Requirements, architecture, and pipeline handoff: [`docs/ingestion/slack.md`](../../docs/ingestion/slack.md).

## Status

Stub HTTP receiver for live events, a local pull for the channels the bot is in, and the `enrich:slack` worker that turns stored deliveries into `source_records` ([Enrich worker](#enrich-worker-ember-67)). The receiver checks Slack's request signature, answers the Request URL challenge, wraps the raw body, and writes the envelope to stdout. No queue yet. The receiver holds no Slack token and does not filter on a workspace or channel.

Live events still need a public HTTPS URL. This change does not open a tunnel or set the app's Request URL.

## Scope

Public and private channels **the Ember bot has been invited to**. A team controls what Ember reads by inviting or removing the bot (`/invite @Ember`, `/remove @Ember`). Direct messages and group DMs are out of scope: the app has no `im:*` or `mpim:*` scopes and subscribes to no DM events.

## Create the Slack app

One Slack app per Ember deployment. A self-hosted team creates its own app in its own workspace, which keeps it an internal app for Slack's rate limits (see the doc).

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest** → pick the workspace.
2. Paste [`slack-app-manifest.json`](slack-app-manifest.json). If the receiver is not reachable yet, delete the `event_subscriptions` block first. Slack checks the Request URL when the manifest is saved.
3. **Install to Workspace.** Copy **OAuth & Permissions → Bot User OAuth Token** (`xoxb-…`) into `SLACK_BOT_TOKEN` and **Basic Information → App Credentials → Signing Secret** into `SLACK_SIGNING_SECRET`.
4. Once a public URL reaches this service, open **Event Subscriptions**, turn it on, set the Request URL to `https://<public-host>/webhook/slack`, and add the bot events from the manifest (or re-apply the manifest with the real URL). Slack sends a `url_verification` request, which the receiver answers.
5. `/invite @Ember` in each channel Ember should read.

The manifest's scopes and events are also `REQUIRED_BOT_SCOPES` and `RECOMMENDED_BOT_EVENTS` in `receiver.py`. `test_webhook.py` fails if the two drift.

## Contract

`POST /webhook/slack` with a JSON object body.

When `SLACK_SIGNING_SECRET` is set, the receiver requires `X-Slack-Request-Timestamp` and `X-Slack-Signature`. The signature is `v0=` plus the hex HMAC-SHA256 of `v0:<timestamp>:<raw body>`, keyed with the signing secret. A timestamp more than five minutes from the receiver's clock is rejected (replay protection), so the host clock must be NTP-synced. Only `v0` is accepted. When the secret is unset, verification is skipped and a warning is logged so a local stub can run. Docs: [Verifying requests from Slack](https://api.slack.com/authentication/verifying-requests-from-slack).

The legacy `token` field inside the body is not used for authentication.

| Request | Response | Emitted |
| --- | --- | --- |
| `url_verification` | `200 {"challenge": "<challenge>"}` | no |
| `event_callback`, `app_rate_limited`, any other object | `200 {"status":"accepted"}` | one line |
| Retry of an `event_id` this process already emitted | `200 {"status":"duplicate"}` | no |
| Bad or missing signature, stale timestamp | `401` | no |
| Not JSON, not an object, challenge missing | `400` | no |

The logged line is exactly:

```json
{"type":"slack","body":{}}
```

`body` is the parsed request JSON, unchanged: the outer `event_callback` wrapper with `team_id`, `api_app_id`, `event_id`, `event_time`, `authorizations`, and the inner `event`. Schema: `packages/ingestion-envelope`.

Slack wants a `2xx` within three seconds and otherwise retries three times with `X-Slack-Retry-Num` and `X-Slack-Retry-Reason`. The receiver does nothing slow before answering. Its duplicate memory holds the last 10,000 `(team_id, event_id)` pairs and is lost on restart, so the pipeline must still de-duplicate on `team_id` + `event_id`.

The process log also records `type`, `event.type`, `event.subtype`, `team_id`, `event_id`, `event.channel`, and the retry number. Missing fields are logged as `None`.

Edits arrive as `message` with `subtype: message_changed` (new text in `event.message`, old in `event.previous_message`). Deletes are `message_deleted` with `deleted_ts`. Thread replies carry `thread_ts`. All stay inside `body` as Slack sent them.

`GET /health` returns `{"status":"ok"}`.

## Credentials

| Variable | Role |
| --- | --- |
| `SLACK_SIGNING_SECRET` | Request signing key for the receiver. Empty skips verification. Not used by `pull_test.py`. |
| `SLACK_INGESTION_PORT` | Receiver port. Default `8082`, beside `github-ingestion` on `8080`. |
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-…`, or `xoxe.xoxb-…` with token rotation on) for `pull_test.py` only. User (`xoxp-`) and app-level (`xapp-`) tokens are refused. The receiver does not read it. Never commit it. |
| `SLACK_TEST_CHANNEL` | One channel id (`C…`/`G…`) for `pull_test.py` only. Empty reads every channel the bot is in. Not a filter on the webhook path. |
| `SLACK_PULL_CONCURRENCY` | Channels read in parallel by `pull_test.py`. Default `4`. Range 1–16. |
| `SLACK_RATE_MODE` | `internal` (default: Slack's normal tiers, for an app the workspace created itself) or `non_marketplace` (history and replies at 1 call/min and 15 per page, for a commercially distributed app not approved for the Slack Marketplace). `pull_test.py` only. |

`bot_token()` in `pull_test.py` is the one place a token is read. When the Application Database holds per-workspace integration settings (CLI/web onboarding, EMBER-12), that function is what changes.

## Run

```sh
python3 services/slack-ingestion/receiver.py
```

Own container, optional compose profile. A plain `up` still starts only Neo4j:

```sh
docker compose -f infra/docker-compose.yml --env-file .env \
  --profile slack-ingestion up --build slack-ingestion
```

### Containers

One image holds both programs (`envelope.py`, `receiver.py`, `pull_test.py`). It runs as `nobody`.

| Compose service | Runs | Lifetime |
| --- | --- | --- |
| `slack-ingestion` | `receiver.py` (the image's default command) | Always on; serves port 8082 |
| `slack-backfill` | `pull_test.py --output /data/slack-intake.jsonl` | One-off. Exits when the backfill finishes. Output goes to the named volume `slack-backfill-data`. |

Run a backfill in a container (reads `SLACK_BOT_TOKEN`, and optionally `SLACK_TEST_CHANNEL`, `SLACK_PULL_CONCURRENCY` and `SLACK_RATE_MODE`, from `.env`). The command includes `--resume`, so re-running it after a crash or restart continues where it stopped:

```sh
docker compose -f infra/docker-compose.yml --env-file .env \
  --profile slack-backfill run --rm --build slack-backfill
```

Pass flags by overriding the command, e.g. one channel since a date:

```sh
docker compose -f infra/docker-compose.yml --env-file .env --profile slack-backfill \
  run --rm slack-backfill python3 pull_test.py --channel C0123ABCD --since 2026-09-01 \
  --output /data/slack-intake.jsonl --resume
```

The base image `python:3.12-slim` is multi-architecture, so the same Dockerfile builds natively on x86_64 and on arm64 hosts such as the DGX Spark (GB10). Checked with `docker buildx build --platform linux/arm64`.

## Local test pull

```sh
git fetch && git checkout feature/EMBER-36-slack-ingestion

export SLACK_BOT_TOKEN="xoxb-..."
# optional: one channel instead of every channel the bot is in
export SLACK_TEST_CHANNEL="C0123ABCD"

python3 services/slack-ingestion/pull_test.py --output "$HOME/ember-slack-intake.jsonl"
```

That writes:

- `$HOME/ember-slack-intake.jsonl`: one EMBER-39 object per line
- `$HOME/ember-slack-intake.jsonl.readable.md`: glance lines (channel, time, author name, short text, reply count; replies indented under their parent)

`--readable PATH`, `--readable -` (needs `--output`), and `--no-readable` work as in the Jira pull. `--channel C…` overrides `SLACK_TEST_CHANNEL`. `--since 2026-09-01` (UTC) or `--since <unix ts>` limits history. `--include-archived` also reads archived channels the bot is in.

### What this cut pulls

One envelope each, in this order:

1. The workspace object from `team.info`.
2. Each member from `users.list`: people, bots, deactivated accounts, with profile email (`users:read.email`) as the join key to GitHub and Jira people.
3. For each channel from `conversations.list` (`types=public_channel,private_channel`, archived excluded unless `--include-archived`) **where `is_member` is true**:
   - the channel object
   - each top-level message from `conversations.history`, oldest first
   - right after a thread parent, each reply from `conversations.replies`. The parent copy Slack repeats at the top of that list is skipped. A reply also broadcast to the channel is emitted once.

The one change to raw objects: history and reply messages have no channel id of their own, so `channel` is set to the channel they were read from when the message does not already carry one. That is the same field name Slack uses on message events. Nothing else is rewritten, dropped, or merged.

Pages follow `response_metadata.next_cursor`, 200 per page (15 in `non_marketplace` mode), with no page ceiling; a cursor seen twice stops the pull. `--since` filters top-level messages, so a thread whose parent is older than `--since` is not read even if it has newer replies. Channels are read 4 at a time. Pages inside one channel stay in order.

### Rate limits (EMBER-52)

Slack limits each Web API method per workspace per app. Each method has its own gate:

- **Pacing:** calls are spaced at the method's tier rate. `team.info`, `conversations.history` and `conversations.replies` run at 50/min (Tier 3), and `users.list` and `conversations.list` at 20/min (Tier 2). Workers don't burst into throttling. `SLACK_RATE_MODE=non_marketplace` (or `--rate-mode`) drops history and replies to 1/min and 15 per page.
- **Throttling is not failure:** a `429`, or an `ok:false` body with `error: "ratelimited"`, waits the full `Retry-After` (at least 1 s, at most 15 min) and retries with no attempt limit. It gives up only when one call has waited an hour in total, which means Slack is limiting harder than the rate mode assumes.
- **Transient errors:** a `5xx`, a timeout, a connection reset or a DNS failure backs off 1, 2, 4, 8, 16, 32, 60 s, each randomly shortened by up to half (jitter) so workers don't retry in lockstep. It stops after 8 attempts.
- **Real errors stop at once:** `invalid_auth`, `token_revoked`, `missing_scope` and other HTTP errors.

### Progress and resume

With `--output`, every page is appended to a part file under `<output>.parts/`, and a checkpoint is saved to `<output>.checkpoint.json` after it. If the run stops (crash, restart, `Ctrl+C`), run the same command with `--resume`, and it continues from the last saved page. The final JSONL is assembled at the end, in the same order as an uninterrupted run, with duplicates from the re-read page removed. Then the checkpoint and part files are deleted.

- `--resume` with no checkpoint starts a fresh run, so it's safe to always pass it. The `slack-backfill` container does.
- Without `--resume`, an existing checkpoint stops the run, so two different runs never mix.
- A checkpoint only resumes with the same `--channel`, `--since` and `--include-archived`, and the same workspace token.
- Without `--output` (JSONL to stdout), the pull runs in memory and can't resume.

Slack reports most failures as HTTP 200 with `{"ok": false, "error": …}`:

- `invalid_auth`, `token_revoked`, and similar stop the pull and say the token was rejected.
- `missing_scope` stops the pull and names the scope to add.
- `not_in_channel` or `channel_not_found` on one channel is logged and skipped, and the rest continues.

If the bot is in no channel, the pull still writes the workspace and members and says to `/invite @Ember`.

### What this cut does not pull

- Direct messages and group DMs.
- Channels the bot is not in, even public ones. `conversations.join` is not used and `channels:join` is not requested.
- File bytes. File metadata stays inside the message that shared it.
- Pins, bookmarks, canvases, huddles, user groups, and emoji.
- It does not replay live events.

## Local webhook smoke

Checks signed deliveries against the receiver on your machine. It does not call Slack and does not need a public URL.

```sh
export SLACK_SIGNING_SECRET="throwaway-local-secret"
scripts/smoke_slack_webhook.sh
```

The script starts the receiver, checks the `url_verification` challenge, POSTs signed `message`, `message_changed`, thread reply, `reaction_added`, and `channel_created` deliveries from two workspaces, then re-sends one as a retry. It expects one stdout line per distinct event and ends with `smoke ok: 5 envelopes` on stderr.

To receive real events later, point a tunnel at port `8082` and set the app's Request URL to `https://<tunnel-host>/webhook/slack`. Examples: `cloudflared tunnel --url http://127.0.0.1:8082` or `ngrok http 8082`. CI does not start a tunnel.

## Enrich worker (EMBER-67)

`enrich.py` is `enrich:slack` from `docs/ingestion/architecture.md`. It reads Slack rows from Postgres `raw_events` (written by intake, EMBER-62), adds what the Events API payload lacks, and writes `source_records`. What each event becomes, what is fetched, and each record's `object_id`, `version` and `occurred_at` are specified in [`docs/ingestion/slack-webhooks.md`](../../docs/ingestion/slack-webhooks.md) (EMBER-70).

| File | What it is |
| --- | --- |
| `enrich.py` | The worker, plus `seed`, which stands in for intake locally |
| `models.py` | SQLAlchemy models for `raw_events`, `source_records` and `rate_limits`, matching the doc's schema |
| `pg_gates.py` | Rate-limit gates kept in table `rate_limits`, used by `pull_test.SlackClient` |
| `requirements.txt` | SQLAlchemy and psycopg 3, for these three files only. The receiver and backfill stay stdlib-only |

### What it does with each row

- **Message events become records.** New messages, replies, thread broadcasts, `message_replied`, edits (`message_changed`, a new version per edit), deletes (`message_deleted`, a tombstone version) and system messages each become one `message` record. Its `record` is the delivery as received (`payload`), the message as of that version (`message`), `deleted`, and the context: `user`, `channel` and `thread_parent`.
- **Context comes from stored records first.** Users and channels are kept as records of kind `user` and `channel`. `users.info` or `conversations.info` is called only when no record exists yet. A reply's parent comes from `conversations.replies` when it isn't stored, and is stored too. A lookup that finds nothing (`user_not_found`, `thread_not_found`, …) leaves that context empty.
- **Everything else is skipped.** Reactions, channel and membership events, `user_change` and `app_rate_limited` are marked enriched and logged as skipped.
- **No duplicates.** Records are inserted with `on conflict (source, tenant, object_id, version) do nothing`, so running a row twice adds nothing.

### How it runs

- **Wake.** Idle, it waits on `LISTEN raw_events`. The database sends `NOTIFY raw_events, 'slack'` on each insert (the trigger EMBER-62 adds). A poll every `ENRICH_POLL_SECONDS` covers anything missed.
- **Claim.** `ENRICH_CONCURRENCY` asyncio runners each claim one pending row (`enriched_at is null`, `FOR UPDATE SKIP LOCKED`). Each one enriches the row, writes its records and sets `enriched_at` in that row's transaction. A crash releases the row, and it runs again.
- **Catch up.** At start it claims straight away, so rows inserted while it was stopped are picked up.
- **Failures.** A failure rolls back the row's records, adds one to `enrich_attempts`, stores `enrich_error`, and backs off. At `ENRICH_MAX_ATTEMPTS` the row is left for a person. `invalid_auth`, `token_revoked` and `missing_scope` jump straight to the maximum.
- **Rate limits.** Every Slack call goes through `SlackClient`, with the pacing, throttle and backoff above ([Rate limits](#rate-limits-ember-52)). Its gates are kept in `rate_limits`, keyed by workspace tenant and method, so every worker process shares one workspace's quota.
- **Stopping.** `Ctrl+C` (or `SIGTERM`) stops claiming and lets the jobs in flight finish. A second `Ctrl+C` exits at once, and an unfinished row stays pending.
- **Tenants.** Rows with `tenant` null (an account not yet mapped) wait.

### Settings

| Variable | Role |
| --- | --- |
| `DATABASE_URL` | The ingestion Postgres. `postgres://`, `postgresql://` and `postgresql+<driver>://` all work, and `sslmode` passes through |
| `SLACK_BOT_TOKEN` | Bot token for the lookups (one workspace for now; per-workspace tokens come with EMBER-12) |
| `SLACK_RATE_MODE` | `internal` (default) or `non_marketplace`, as for the backfill |
| `ENRICH_CONCURRENCY` | Rows at once, 1–16. Default `4` |
| `ENRICH_POLL_SECONDS` | Fallback poll. Default `30` |
| `ENRICH_MAX_ATTEMPTS` | Tries before a row is left for a person. Default `8` |
| `ENRICH_CHANNEL` | The `LISTEN` channel. Default `raw_events` |

### Run it locally

Start Docker, then a local Postgres migrated exactly like the cluster's (same version, same migration files):

```sh
docker compose -f infra/docker-compose.yml --env-file .env --profile postgres up -d postgres postgres-migrate
```

Run the worker on the host:

```sh
python3 -m venv .venv && .venv/bin/pip install -r services/slack-ingestion/requirements.txt
export DATABASE_URL='postgresql://ember:change-me-locally@127.0.0.1:5432/ember?sslmode=disable'
export SLACK_BOT_TOKEN='xoxb-...'
.venv/bin/python services/slack-ingestion/enrich.py
```

Or run it in compose, where it also waits for the migrations: `docker compose -f infra/docker-compose.yml --env-file .env --profile slack-enrich up --build slack-enrich`.

Until intake exists, store deliveries with `seed`. It inserts `raw_events` rows (`delivery_id` = `event_id`, `event_name` = `type[:subtype]`) and sends the NOTIFY itself:

```sh
python3 services/slack-ingestion/enrich.py seed packages/ingestion-envelope/fixtures/slack.json --tenant dev
# live: whatever the receiver gets goes straight in
python3 services/slack-ingestion/receiver.py | python3 services/slack-ingestion/enrich.py seed - --tenant dev
```

On Windows, use `python` instead of `python3`; the worker picks the event loop psycopg needs there.

## Test

```sh
python3 services/slack-ingestion/test_webhook.py
python3 services/slack-ingestion/test_pull.py
python3 services/slack-ingestion/test_enrich.py       # needs requirements.txt
PGTEST_URL='postgresql://ember:change-me-locally@127.0.0.1:5432/postgres?sslmode=disable' \
  python3 services/slack-ingestion/test_enrich_db.py  # needs a Postgres it may create a database on
```

`test_pull.py` and `test_enrich.py` answer Slack Web API calls from in-memory fakes. None of the tests call Slack.

`test_enrich_db.py` builds database `ember_test_enrich` from `infra/k8s/apps/postgres/migrations`. It adds a test-only stand-in for EMBER-62's NOTIFY trigger, then checks EMBER-67's acceptance criteria against the real worker:
- records with context
- no duplicates
- a wake within 1 s of an insert
- catch-up after a stop
- concurrency
- `rate_limits`
- Ctrl+C finishing the job in flight
- retries and permanent errors

CI runs all four, plus the smoke.

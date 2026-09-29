# services/slack-ingestion

Slack Events API connector. Each delivery is emitted as EMBER-39 intake: `{"type":"slack","body":<raw JSON object>}`.

Jira: EMBER-36. Parent epic: EMBER-7. Floor. The worker is workspace-agnostic. It does not clean, sort, filter, or reconstruct threads. That is the Slack source pipeline's job (EMBER-7). Embeddings stay out (EMBER-3).

Requirements, architecture, and pipeline handoff: [`docs/ingestion/slack.md`](../../docs/ingestion/slack.md).

## Status

Stub HTTP receiver for live events, plus a local pull for the channels the bot is in. The receiver checks Slack's request signature, answers the Request URL challenge, wraps the raw body, and writes the envelope to stdout. No queue yet. The receiver holds no Slack token and does not filter on a workspace or channel.

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
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-…`) for `pull_test.py` only. User (`xoxp-`) and app-level (`xapp-`) tokens are refused. The receiver does not read it. Never commit it. |
| `SLACK_TEST_CHANNEL` | One channel id (`C…`/`G…`) for `pull_test.py` only. Empty reads every channel the bot is in. Not a filter on the webhook path. |
| `SLACK_PULL_CONCURRENCY` | Channels read in parallel by `pull_test.py`. Default `4`. Range 1–16. |

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

Pages follow `response_metadata.next_cursor`, 200 per page, up to 50 pages per list. Channels are read 4 at a time. Pages inside one channel stay in order.

Slack rate-limits each Web API method on its own. Each method has its own gate: a `429` holds every worker calling that method for `Retry-After` seconds and leaves the rest running. A `5xx` backs off 1s, 2s, 4s. After five throttled attempts the pull stops.

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

## Test

```sh
python3 services/slack-ingestion/test_webhook.py
python3 services/slack-ingestion/test_pull.py
```

`test_pull.py` answers Slack Web API calls from an in-memory fake. CI runs both, plus the smoke. None of them call Slack.

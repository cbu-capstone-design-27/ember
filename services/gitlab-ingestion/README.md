# services/gitlab-ingestion

GitLab webhook receiver plus REST backfill and reconciliation. Every object is emitted as EMBER-39 intake: `{"type":"gitlab","body":<raw JSON object>}`.

Jira: EMBER-54 (connector) and EMBER-55 (rate limits), epic EMBER-20. Design: [`docs/ingestion/gitlab.md`](../../docs/ingestion/gitlab.md). The worker is project- and group-agnostic. It does not clean or sort the payload. Embeddings stay out (EMBER-3).

## Status

- `receiver.py` takes live webhook deliveries. It checks the configured webhook secrets, wraps the raw body, and writes one envelope per delivery to stdout. It does not filter on event, project, or group, holds no API token, and never calls GitLab. GitLab webhooks carry the full object, so nothing needs fetching on the live path.
- `pull.py` is the backfill and reconciliation. It reads one project through REST API v4 and writes one envelope per REST object.
- Both run from one Docker image, configured only through environment variables.
- No queue yet. Live webhooks still need a public URL (EMBER-40). This change does not open a tunnel or register a webhook.

## Webhook

### Contract

`POST /webhook/gitlab` with a JSON object body. The logged line is exactly:

```json
{"type":"gitlab","body":{}}
```

`body` is the parsed webhook JSON, unchanged. Schema: `packages/ingestion-envelope`.

The receiver answers `200` fast, as GitLab asks (10 seconds on GitLab.com). Four failures in a row disable a webhook for a while, and forty disable it for good, so nothing slow runs in the request path. Bodies up to 25 MiB are accepted, GitLab.com's payload limit, so a large delivery is never answered with a `413` failure.

`webhook-id` (the same value as `Idempotency-Key`) is stable across retries. A delivery already emitted by this process gets `200 {"status":"duplicate"}` and is not emitted again. That memory is lost on restart, so the pipeline still upserts on stable ids.

The process log records `X-Gitlab-Event`, `X-Gitlab-Event-UUID`, `X-Gitlab-Instance`, `object_kind`, project id and path, and `iid` for later routing. Missing fields are logged as empty.

`GET /health` returns `{"status":"ok"}`.

### Verification

Set either secret, or both, on the GitLab webhook and in the environment. Every secret that is set is checked.

| Variable | GitLab setting | What the receiver checks |
| --- | --- | --- |
| `GITLAB_WEBHOOK_SIGNING_TOKEN` | Signing token (recommended). Looks like `whsec_...` | `webhook-signature` is `v1,<base64 HMAC-SHA256>` of `<webhook-id>.<webhook-timestamp>.<raw body>`, keyed with the base64-decoded token after `whsec_`. `webhook-timestamp` must be within 5 minutes. A malformed token stops the receiver at startup. |
| `GITLAB_WEBHOOK_SECRET_TOKEN` | Secret token (legacy) | `X-Gitlab-Token` equals it. Weaker, but common on older and self-managed instances. |

With neither set, verification is skipped and a warning is logged so a local stub can run.

### Triggers

Enable these on the project or group webhook. Each delivery is still one raw envelope, whatever the event is. The list lives in `RECOMMENDED_WEBHOOK_TRIGGERS` in `receiver.py`.

| Trigger | `X-Gitlab-Event` | Backfill equivalent |
| --- | --- | --- |
| Merge request events | `Merge Request Hook` | Merge requests |
| Issues events | `Issue Hook`, `Work Item Hook` | Issues |
| Comments | `Note Hook` | Merge request and issue notes |

Confidential issues events and confidential comments are separate triggers and stay off for the first cut. Push, tag, pipeline, job, wiki, release and milestone triggers are deferred.

## Backfill and reconciliation

### Credentials

| Variable | Role |
| --- | --- |
| `GITLAB_BASE_URL` | Instance URL, without `/api/v4`. Default `https://gitlab.com`. Must be https, except for a local instance on `localhost`. |
| `GITLAB_API_TOKEN` | Access token with the `read_api` scope. Prefer a group access token: it belongs to the group, not a person. Sent as `PRIVATE-TOKEN`. Never commit it. |
| `GITLAB_PROJECT` | Project to pull: numeric id or full path (`group/project`). `--project` overrides it. |
| `GITLAB_PULL_CONCURRENCY` | Parallel note fetches. Default 8, maximum 32. `--concurrency` overrides it. |

The receiver never reads these, and the compose file only gives them to the backfill service.

### What it pulls

For one project, in JSONL order:

1. The project (`GET /projects/:id`).
2. Merge requests, every state (`GET /projects/:id/merge_requests?state=all`).
3. Notes on each merge request (`GET /projects/:id/merge_requests/:iid/notes`), including system notes such as approvals.
4. Issues, every state (`GET /projects/:id/issues?state=all&scope=all`).
5. Notes on each issue (`GET /projects/:id/issues/:iid/notes`).

List objects are emitted as GitLab returns them. They already carry the description, state, author, assignees, reviewers and labels the pipeline needs, so there is no second GET per object.

Confidential issues and internal notes are skipped, matching the webhook triggers that stay off. The summary counts what was skipped.

Not pulled: commits, branches, tags, pipelines, releases, milestones, labels as objects, wiki, members, and merge request diffs. Those follow the deferred triggers.

Lists are paged 100 at a time through the `Link` header and stop with an error after 200 pages (20,000 objects) instead of truncating quietly. A pagination link that leaves the configured instance's API is refused, so the token is never sent anywhere else.

### Reconciliation

`--updated-after 2026-10-01T00:00:00Z` or `--lookback-hours 24` pulls the project plus only the merge requests and issues updated after the cutoff, with their notes. A new note bumps its parent's `updated_at`, so new comments are covered. Run it on a schedule to re-cover anything a disabled webhook dropped. Overlapping windows are fine: the pipeline upserts on stable ids. The schedule itself belongs to the worker architecture (EMBER-41).

### Rate limits

GitLab.com allows 2,000 authenticated API requests a minute per user (checked against docs.gitlab.com on 2026-10-01). Self-managed instances set their own limits. Every response carries `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` (a Unix time). A throttled `429` adds `Retry-After` in seconds.

`pull.py` uses one gate shared by every worker:

- **Before the limit:** when `RateLimit-Remaining` drops to 50 or fewer (or a tenth of a smaller limit), every worker pauses until `RateLimit-Reset`.
- **On a 429:** wait out `Retry-After`, or `RateLimit-Reset` when it is missing, then retry.
- **On a 429/502/503/504 with neither header, or a network error:** back off 1s, 2s, 4s, 8s.
- Each wait is capped at 120 seconds. After five attempts the request fails with the GitLab status.

### Run

```sh
export GITLAB_API_TOKEN="your-read_api-token"
export GITLAB_PROJECT="group/project"

python3 services/gitlab-ingestion/pull.py --output "$HOME/ember-gitlab-intake.jsonl"
```

That writes:

- `$HOME/ember-gitlab-intake.jsonl`: one EMBER-39 object per line, the same as stdout without `--output`
- `$HOME/ember-gitlab-intake.jsonl.readable.md`: envelope counts, skipped counts, then a glance line per object (`!iid` or `#iid`, title, state, a short description; note author and a short comment)

`--readable PATH` chooses a different summary file. `--readable -` prints the summary on stdout and requires `--output` so the JSONL stays in the file. `--no-readable` writes JSONL only. Progress goes to stderr.

## Container

One image, two compose services, both outside the default `up`:

```sh
# Webhook receiver on GITLAB_INGESTION_PORT (default 8083). Gets only the webhook secrets.
docker compose -f infra/docker-compose.yml --env-file .env \
  --profile gitlab-ingestion up --build gitlab-ingestion

# One-off backfill to a local file. Gets only the API token.
docker compose -f infra/docker-compose.yml --env-file .env \
  --profile gitlab-backfill run --rm -T gitlab-backfill > gitlab.jsonl

# Reconciliation: arguments after the service name go to pull.py.
docker compose -f infra/docker-compose.yml --env-file .env \
  --profile gitlab-backfill run --rm -T gitlab-backfill --lookback-hours 24 > gitlab.jsonl
```

The image is `python:3.12-slim`, stdlib only, and runs as `nobody`. CI builds it and checks `/health` on a running container.

To receive a real delivery before the cluster tunnel exists, point a temporary tunnel at port 8083 (for example `cloudflared tunnel --url http://127.0.0.1:8083`) and set the webhook URL to `https://<tunnel-host>/webhook/gitlab`.

## Local webhook smoke

Checks signed deliveries against the receiver on your machine. It does not call GitLab and does not need a public URL.

```sh
export GITLAB_WEBHOOK_SIGNING_TOKEN="whsec_$(printf 'throwaway-local-key' | base64)"

scripts/smoke_gitlab_webhook.sh
```

The script starts the receiver and POSTs signed Merge Request, Issue, Note and Work Item hooks, one retried duplicate, and one forged delivery. It expects `200` for each hook, `duplicate` for the retry, `401` for the forgery, and one stdout line per distinct hook. Success ends with `smoke ok: 4 envelopes` on stderr.

## Test

```sh
python3 services/gitlab-ingestion/test_webhook.py
python3 services/gitlab-ingestion/test_pull.py
GITLAB_WEBHOOK_SIGNING_TOKEN=whsec_Y2ktbG9jYWwtc21va2Uta2V5 python3 services/gitlab-ingestion/smoke_webhook.py
```

`test_pull.py` mocks GitLab, including paging and rate-limit headers, with a fake clock. CI runs all three and builds the image. None of them call GitLab.

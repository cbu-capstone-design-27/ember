# services/jira-ingestion

Jira admin-webhook connector. Each delivery is emitted as EMBER-39 intake: `{"type":"jira","body":<raw JSON object>}`.

Jira: EMBER-31. Parent epic: EMBER-6. Floor scaffold. The worker is site- and project-agnostic. It does not clean or sort the payload. Embeddings stay out (EMBER-3). No live tunnel and no webhook registration in this change.

Which projects feed Ember is a later webapp concern, not this service. A JQL filter, if you add one, lives on the Jira webhook. This process does not read it.

## Status

Stub HTTP receiver for live deliveries, plus a local pull command for one test project. The receiver checks an optional HMAC, wraps the raw body, and writes the envelope to stdout. No queue yet. The receiver does not call the Jira API and does not filter on a site or project.

Live admin webhooks still need a public HTTPS URL. This change does not open a tunnel or register a webhook.

Atlassian MCP is not a runtime path. It is not imported, not called, and not a substitute for the webhook or the REST pull.

## Contract

`POST /webhook/jira` with a JSON object body.

When `JIRA_WEBHOOK_SECRET` is set, the receiver requires `X-Hub-Signature` (`sha256=` plus the hex HMAC-SHA256 of the raw body). When the secret is unset, verification is skipped and a warning is logged so a local stub can run.

That header is what Jira Cloud sends for an admin webhook once a secret is saved on the webhook (Jira Administration → System → Webhooks, or `POST /rest/webhooks/1.0/webhook`). Atlassian documents the check as WebSub `method=signature`. The published test vector is secret `It's a Secret to Everybody`, payload `Hello World!`, header `sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9`. Only `sha256` is accepted. Docs: [Jira Cloud webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/) (Secure admin webhooks).

A `?secret=` query parameter does not authenticate. Jira uses the secret as the HMAC key. It does not append the secret to the URL. Putting the secret in the query string would also land it in access logs.

The logged line is exactly:

```json
{"type":"jira","body":{}}
```

`body` is the parsed webhook JSON, unchanged. Schema: `packages/ingestion-envelope`. Description text, ADF, or `null` stays inside that object. This worker does not flatten it.

When the payload includes them, the process log also records `cloudId`, `issue.fields.project.key` (or a top-level `project.key`), and `issue.key` for later routing. Admin webhook bodies often omit `cloudId`. Missing fields are logged as empty. The receiver never filters on a configured site or project.

`GET /health` returns `{"status":"ok"}`.

## What this cut is

An admin webhook receiver, same shape as the GitHub App receiver: verify, wrap, stdout. Backfill is a separate local script.

Registration later, once a public URL exists:

1. Jira Administration → System → Webhooks → Create a webhook. The same record can be created with `POST /rest/webhooks/1.0/webhook`.
2. URL: `https://<public-host>/webhook/jira`. Jira only delivers admin webhooks to an allow-listed port (`443`, `8080`, `8443`, and the rest of the list in the Atlassian doc). The local listener defaults to `8081` so it can sit beside `github-ingestion` on `8080`. A tunnel should terminate TLS on `443` and forward to `8081`. This PR does not start a tunnel.
3. Leave "Exclude body" off so the issue object is in the POST.
4. Events so a live delivery can carry the same entities the backfill stores. The receiver accepts any JSON object and does not filter on `webhookEvent`.

   Subscribe to:

   - `jira:issue_created`, `jira:issue_updated`, `jira:issue_deleted`
   - `comment_created`, `comment_updated`, `comment_deleted`
   - `worklog_created`, `worklog_updated`, `worklog_deleted`
   - `attachment_created`, `attachment_deleted`
   - `issuelink_created`, `issuelink_deleted`
   - `issue_property_set`, `issue_property_deleted`

   `jira:issue_updated` is also where Jira puts the changelog for that edit. There is no separate changelog webhook. Watchers, votes, and remote links have no admin webhook event; those JSON objects are backfill-only. Version events (`jira:version_created` and the other `jira:version_*` names) exist if you also want version records live. Component records do not have their own webhook.
5. JQL: empty receives every project on that site. A JQL filter is optional and is Jira-side. Do not hard-code a project into this service.
6. Generate a secret, store it only in `JIRA_WEBHOOK_SECRET`, and save it on the webhook. Jira will not show the secret again. `isSigned` on the webhook record is true when a secret is set.

Forge apps, Atlassian Connect (JWT `sharedSecret`), and OAuth 2.0 dynamic webhooks (`/rest/api/3/webhook`, bearer token) are a different registration path. This worker does not implement them. Polling is not the live path. `pull_test.py` is a local backfill only.

## Credentials

| Variable | Role |
| --- | --- |
| `JIRA_BASE_URL` | Site origin for `pull_test.py`, such as `https://<site>`. No path. The webhook receiver does not read it. |
| `JIRA_EMAIL` | Atlassian account email for basic auth. `pull_test.py` only. |
| `JIRA_API_TOKEN` | API token for basic auth. `pull_test.py` only. Never commit it. |
| `JIRA_WEBHOOK_SECRET` | HMAC key for `X-Hub-Signature`. Empty skips verification. Not used by `pull_test.py`. |
| `JIRA_INGESTION_PORT` | Receiver port. Default `8081`. |
| `JIRA_TEST_PROJECT` | Project key for `pull_test.py` only. When unset, the script uses `EMBER`. Not a filter on the webhook path. |
| `JIRA_PULL_CONCURRENCY` | Parallel REST GETs for `pull_test.py`. Default `32`. Range 1–64. |

Create an API token at [Atlassian account security](https://id.atlassian.com/manage-profile/security/api-tokens). The token is a password for basic auth (`email:token`). It is not the webhook secret.

## Run

```sh
python3 services/jira-ingestion/receiver.py
```

`JIRA_INGESTION_PORT` defaults to `8081`.

Own container, optional compose profile. A plain `up` still starts only Neo4j:

```sh
docker compose -f infra/docker-compose.yml --env-file .env \
  --profile jira-ingestion up --build jira-ingestion
```

## Local test pull

After checkout:

```sh
git fetch && git checkout feature/EMBER-31-jira-ingestion-worker

export JIRA_BASE_URL="https://<your-site>.atlassian.net"
export JIRA_EMAIL="you@example.com"
export JIRA_API_TOKEN="<api-token>"
export JIRA_TEST_PROJECT="EMBER"

python3 services/jira-ingestion/pull_test.py
```

`JIRA_TEST_PROJECT` may be omitted. The default is `EMBER`. `--project WIDGET` overrides it for that run.

That writes JSONL to stdout, one envelope per object:

```json
{"type":"jira","body":{}}
```

`body` is the raw JSON from that GET. On an issue, Jira Cloud API v3 stores `body.fields.description` as an Atlassian document (ADF), a string, or JSON `null`. The script does not rewrite it. A comment or worklog is its own line, not a field stripped off the issue.

To keep that JSONL in a file and also get a glance summary:

```sh
python3 services/jira-ingestion/pull_test.py --output "$HOME/ember-jira-intake.jsonl"
```

That writes:

- `$HOME/ember-jira-intake.jsonl` — one EMBER-39 object per line, same as stdout
- `$HOME/ember-jira-intake.jsonl.readable.md` — glance lines with human text. An issue line is the key, status, summary, and a short description. A comment line is the id, author, and a short body. Long text is cut off. The JSONL line is still the full raw object, including the ADF description.

`--readable PATH` chooses a different summary file. `--readable -` prints the summary on stdout and requires `--output` so the JSONL stays in the file. `--no-readable` writes JSONL only.

### What this cut pulls

For the one test project, JQL `project = <KEY> ORDER BY key ASC` via `POST /rest/api/3/search/jql` (`fields` is `key` only, pages of 100). The legacy `/rest/api/3/search` endpoint is not called. Subtasks in that project are issues and are included.

Then, one envelope each:

- `GET /rest/api/3/project/{key}`
- each component from `GET /rest/api/3/project/{key}/components`
- each version from `GET /rest/api/3/project/{key}/version`
- each issue-type status set from `GET /rest/api/3/project/{key}/statuses`
- each project property from `GET /rest/api/3/project/{key}/properties/{propertyKey}`
- `GET /rest/api/3/issue/{key}?fields=*all` (description and every other field Jira returns, including custom fields)
- each object in `fields.issuelinks`
- each comment from `GET /rest/api/3/issue/{key}/comment`
- each changelog page from `GET /rest/api/3/issue/{key}/changelog`. That response is a page bean: entries are in `values`. `histories` (the expand=changelog shape) is accepted too. The page is stored unchanged. A forbidden response, or a page with neither list, is logged and skipped for that issue. The rest of the pull continues.
- each worklog from `GET /rest/api/3/issue/{key}/worklog`
- each attachment's metadata from `GET /rest/api/3/attachment/{id}`
- each remote link from `GET /rest/api/3/issue/{key}/remotelink`
- `GET /rest/api/3/issue/{key}/watchers` and `GET /rest/api/3/issue/{key}/votes` when Jira returns them
- each issue property from `GET /rest/api/3/issue/{key}/properties/{propertyKey}`

The issue object still contains Jira's own comment and worklog previews. Those previews are not removed. The complete comment and worklog lists are additional envelopes. A comment or worklog total of 0 skips that list GET.

Search pages stay one-after-another. Detail GETs run 32 at a time, across issues and across comments, changelog pages, worklogs, and the other resources. Later pages are requested together, then written back in `startAt` order. Tune with `--concurrency N` or `JIRA_PULL_CONCURRENCY` (1–64). If Jira returns `Retry-After`, the script waits that long and retries. A `429` / `502` / `503` without `Retry-After` waits 1s, then 2s, then 4s, and so on.

A `403` or `404` on watchers, votes, or one issue's changelog is skipped for that issue. A changelog body with neither `values` nor `histories` is skipped the same way. Every other GET failure stops the pull.

### What this cut does not pull

- Attachment file bytes and thumbnails (`/attachment/content/`, `/attachment/thumbnail/`). Metadata only.
- `renderedFields` HTML. The stored description on the issue is the ADF (or string) Jira returned.
- Agile board and sprint APIs. A sprint value is included only when it is already on the issue under `fields=*all`.
- The development panel (`/rest/dev-status/...`). That is a separate product API, not the issue REST resource.
- Transitions, editmeta, permission schemes, notification schemes, workflows, and avatars.
- Any project other than the one selected for that run. It does not replay webhooks. Jira has no pull requests.

Search and each child resource stop after 50 pages.

## Local webhook smoke

This checks a signed delivery against the receiver on your machine. It does not call Jira and it does not need a public URL. `pull_test.py` is a separate backfill.

```sh
git fetch && git checkout feature/EMBER-31-jira-ingestion-worker

export JIRA_WEBHOOK_SECRET="throwaway-local-secret"

scripts/smoke_jira_webhook.sh
```

Any throwaway string is fine. The script starts the receiver and POSTs signed deliveries for `jira:issue_created`, `jira:issue_updated`, `comment_created`, `worklog_created`, and `attachment_created`. It expects HTTP 202 plus one stdout line per delivery. The issue samples use different `cloudId` and project key values. Description, comment body, and worklog note stay inside `body`.

Success ends with `smoke ok: 5 envelopes` on stderr.

To receive a real admin delivery later, point a tunnel at `JIRA_INGESTION_PORT` (default `8081`) and set the webhook URL to `https://<tunnel-host>/webhook/jira` with the same secret. Examples: `cloudflared tunnel --url http://127.0.0.1:8081` or `ngrok http 8081`. The tunnel is not part of this smoke and CI does not start one. Jira will not POST to port `8081` directly. Terminate TLS on port `443`.

## Test

```sh
python3 services/jira-ingestion/test_webhook.py
python3 services/jira-ingestion/test_pull.py
```

`test_pull.py` mocks HTTP. CI runs both, plus the smoke script. None of them call Jira.

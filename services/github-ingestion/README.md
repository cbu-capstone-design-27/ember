# services/github-ingestion

GitHub App webhook connector. Each delivery is emitted as EMBER-39 intake: `{"type":"github","body":<raw JSON object>}`.

Jira: EMBER-35. Floor scaffold. The worker is repo- and account-agnostic. It does not clean or sort the payload. Embeddings stay out (EMBER-3). No live tunnel and no App registration in this change.

Installing the App on an account or org defaults to all repositories. Choosing which of those repositories feed Ember is a later webapp concern, not this service.

## Status

Stub HTTP receiver for live deliveries, plus a local pull command for one test repository. The receiver checks an optional HMAC, wraps the raw body, and writes the envelope to stdout. No queue yet. The receiver does not call the GitHub API and does not filter on a repository.

Live App webhooks still need a public URL. This change does not open a tunnel or register a webhook.

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

## GitHub App

This is a GitHub App webhook endpoint, not a classic OAuth app and not a single-repo webhook. `ember-ingest` is an example App. The worker does not hard-code its slug, id, or install target.

Permissions below are what `pull_test.py` needs on the installation. Event subscriptions are for live webhooks later; the receiver does not filter on them, and this PR does not register them.

### Credentials

| Variable | Role |
| --- | --- |
| `GITHUB_APP_ID` | App id. Example: `5075660` (`ember-ingest`). Not a secret. The webhook receiver does not read it. `pull_test.py` does. |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Path to the App `.pem`. Preferred. Never commit the file. |
| `GITHUB_APP_PRIVATE_KEY` | PEM contents, including the BEGIN/END lines. Used by `pull_test.py` only when the path is empty. |
| `GITHUB_WEBHOOK_SECRET` | Used by the webhook receiver for HMAC. Empty skips verification. Not used by `pull_test.py`. |
| `GITHUB_TEST_REPO` | `owner/name` for `pull_test.py` only. Example test target: `ryan-stoffel/photon`. Not a filter on the webhook path. |

### Permissions (read)

Repository permissions this cut uses:

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

## Local test pull

Example App already created (not hard-coded in the worker):

- Slug: `ember-ingest`
- App ID: `5075660`
- Client ID: `Iv23liDrl4PFQRt0KB4t`
- Org: `cbu-capstone-design-27`
- Installed on the current test target: `ryan-stoffel/photon`

The private key and webhook secret stay on your machine.

After checkout:

```sh
git fetch && git checkout feature/EMBER-35-github-ingestion-worker

export GITHUB_APP_ID=5075660
export GITHUB_APP_PRIVATE_KEY_PATH="/absolute/path/to/ember-ingest.private-key.pem"
export GITHUB_WEBHOOK_SECRET="your-webhook-secret"
export GITHUB_TEST_REPO=ryan-stoffel/photon

python3 services/github-ingestion/pull_test.py
```

That writes JSONL to stdout, one envelope per item:

```json
{"type":"github","body":{}}
```

For issues and pull requests, `body` inside that object is the full GitHub payload from `GET /repos/{owner}/{repo}/issues/{number}` or `GET /repos/{owner}/{repo}/pulls/{number}`. That payload's `body` field is the description text (JSON `null` when the description is empty). Commits stay the Commits API list object, including `commit.message` when GitHub sent it.

To keep that JSONL in a file and also get a glance summary:

```sh
python3 services/github-ingestion/pull_test.py --output "$HOME/ember-github-intake.jsonl"
```

That writes:

- `$HOME/ember-github-intake.jsonl` — one EMBER-39 object per line, same as stdout
- `$HOME/ember-github-intake.jsonl.readable.md` — issues (`#`, title, state, short description), pull requests (`#`, title, state, short description), and commits (short sha, subject), grouped under headings

`--readable PATH` chooses a different summary file. `--readable -` prints the summary on stdout and requires `--output` so the JSONL stays in the file. `--no-readable` writes JSONL only.

The summary lists each issue, pull request, and commit once. The JSONL still contains every raw API object, so a pull request can appear twice there (Issues API and Pulls API). A merged pull request is labeled `merged` in the summary.

`--repo owner/name` overrides `GITHUB_TEST_REPO` for that run.

RS256 is not in the Python standard library. The script signs the App JWT with `openssl dgst -sha256 -sign` and does not install a crypto package. `openssl` must be on `PATH`.

### What this cut pulls

For the one test repo:

- Issues API list (`state=all`) to find numbers, then `GET /repos/{owner}/{repo}/issues/{number}` for each issue. A list row that is a pull request is fetched with the pull request URL instead.
- Pulls API list (`state=all`), then `GET /repos/{owner}/{repo}/pulls/{number}` for each one.
- Commits API list on the default branch (100 per page). Those list objects are emitted as returned.

List pages and commit pages stay one-after-another. Issue and pull-request detail GETs run 12 at a time, then the JSONL is written in the same order as a serial pull. The envelopes are the same objects as before. Tune with `--concurrency N` or `GITHUB_PULL_CONCURRENCY` (1–32). If GitHub returns `Retry-After`, or a primary rate limit with `x-ratelimit-remaining: 0`, the script waits and retries that request.

### What this cut does not pull

Issue comments, pull request reviews, review comments, check runs, statuses, releases, file contents, diffs, or any repository other than `GITHUB_TEST_REPO`. It does not replay webhooks.

## Test

```sh
python3 services/github-ingestion/test_webhook.py
python3 services/github-ingestion/test_pull.py
```

`test_pull.py` mocks HTTP. CI runs both. Neither test calls GitHub.

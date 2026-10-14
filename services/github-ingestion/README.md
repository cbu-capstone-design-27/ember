# services/github-ingestion

GitHub App webhook connector. Each delivery is emitted as EMBER-39 intake: `{"type":"github","body":<raw JSON object>}`.

Jira: EMBER-35. Floor scaffold. The worker is repo- and account-agnostic. It does not clean or sort the payload. Embeddings stay out (EMBER-3). No live tunnel and no App registration in this change.

Rate limits for a GitHub App installation token and for a personal access token, and how the backfill waits them out: [`docs/ingestion/github.md`](../../docs/ingestion/github.md) (EMBER-44).

Installing the App on an account or org defaults to all repositories. Choosing which of those repositories feed Ember is a later webapp concern, not this service.

## Status

HTTP receiver for live deliveries, plus a local full REST backfill for one test repository. The receiver checks an optional HMAC, wraps the raw body, and writes one envelope per delivery to stdout. It does not filter on event name or repository. No queue yet. The receiver does not call the GitHub API.

`pull_test.py` is the backfill. It is a follow-on to the slim issues / pull requests / default-branch commits cut: one envelope per discrete REST object, not a second copy of the same pull request.

Live App webhooks still need a public URL that reaches this receiver. The cluster's webhook URL ends at a placeholder for now (see the end of "Local webhook smoke"). This change does not open a tunnel or register a webhook. Subscribe the events below on the App when you want those deliveries.

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

Permissions below are what `pull_test.py` needs on the installation. Event subscriptions are for live webhooks; the receiver does not filter on them, and this PR does not register them. The list lives in `RECOMMENDED_WEBHOOK_EVENTS` in `receiver.py`.

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

### Events

Subscribe to these on the App. Each delivery is still one raw envelope, whatever the event name is.

- `installation`
- `installation_repositories`
- `repository`
- `push`
- `create`
- `delete`
- `issues`
- `issue_comment`
- `pull_request`
- `pull_request_review`
- `pull_request_review_comment`
- `commit_comment`
- `release`
- `milestone`
- `label`
- `status`
- `check_run`
- `check_suite`
- `workflow_run`
- `workflow_job`
- `deployment`
- `deployment_status`
- `discussion`
- `discussion_comment`

| Live event | What the REST backfill stores for the same idea |
| --- | --- |
| `issues` | Issue resource, full GET, open and closed |
| `issue_comment` | Issue comments, including conversation comments on pull requests |
| `pull_request` | Pull request resource, full GET |
| `pull_request_review` | Reviews |
| `pull_request_review_comment` | Review comments (diff line comments) |
| `push` | Unique commits from every branch and from pull requests |
| `create`, `delete` | Branch tip refs and tags |
| `release` | Releases |
| `commit_comment` | Commit comments |
| `milestone` | Milestones |
| `label` | Labels |
| `discussion`, `discussion_comment` | Not in the REST backfill (no REST collection) |
| `check_run`, `check_suite`, `status` | Not in the REST backfill (per commit, extra permission) |
| `workflow_run`, `workflow_job` | Not in the REST backfill (Actions permission) |
| `deployment`, `deployment_status` | Not in the REST backfill (Deployments permission) |

The receiver accepts any JSON object. It does not filter on event name.

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
git fetch && git checkout feature/EMBER-35-github-full-backfill

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

Each line is one discrete REST object. For issues and pull requests, the envelope `body` is the full GitHub payload from `GET /repos/{owner}/{repo}/issues/{number}` or `GET /repos/{owner}/{repo}/pulls/{number}`. That payload's `body` field is the description text (JSON `null` when the description is empty). A pull request is two envelopes: the issue resource and the pull resource. Comments, reviews, commits, releases, and tags are the list objects for those routes (those objects already include the comment, review, or message). Commits include `commit.message` when GitHub sent it. Each commit SHA appears once.

To keep that JSONL in a file and also get a glance summary:

```sh
python3 services/github-ingestion/pull_test.py --output "$HOME/ember-github-intake.jsonl"
```

That writes:

- `$HOME/ember-github-intake.jsonl` — one EMBER-39 object per line, same as stdout
- `$HOME/ember-github-intake.jsonl.readable.md` — envelope counts, then a glance line per object. Issues and pull requests show `#`, title, state, and a short description. Comments and review comments show the author and a short comment, not the comment id. Commits show the short sha and subject (the author, when the subject is empty). It also records how many duplicate commit SHA hits were left out of the JSONL. The JSONL objects stay the full raw payloads.

`--readable PATH` chooses a different summary file. `--readable -` prints the summary on stdout and requires `--output` so the JSONL stays in the file. `--no-readable` writes JSONL only.

The summary lists every stored object once, under the entity it was fetched as. A pull request therefore shows under Issues (issue resource) and under Pull requests (pull resource). A merged pull request is labeled `merged` on the pull resource. Commit lines are the unique objects. The duplicate-SHA count is the branch and pull-request hits that were not written again.

`--repo owner/name` overrides `GITHUB_TEST_REPO` for that run.

RS256 is not in the Python standard library. The script signs the App JWT with `openssl dgst -sha256 -sign` and does not install a crypto package. `openssl` must be on `PATH`.

### What this cut pulls

For the one test repo, in JSONL order:

1. Repository (`GET /repos/{owner}/{repo}`).
2. Labels.
3. Milestones (`state=all`).
4. Issues, open and closed. List to find numbers, then `GET /repos/{owner}/{repo}/issues/{number}` for each, including pull requests as issue resources.
5. Issue comments, repo-wide (`GET /issues/comments`). This includes conversation comments on pull requests.
6. Pull requests, open and closed. List, then `GET /repos/{owner}/{repo}/pulls/{number}` for each.
7. Pull request reviews (`GET /pulls/{number}/reviews`). There is no repo-wide reviews route.
8. Pull request review comments, repo-wide (`GET /pulls/comments`). These are diff line comments, not the conversation comments in step 5.
9. Branches as tip refs (`GET /branches`). Default branch first, then name order.
10. Commits on every branch (`GET /commits?sha={branch}`), then commits on each pull request (`GET /pulls/{number}/commits`) so a fork-only SHA is kept. Each SHA is written once. Listing still requests every branch; dedupe is on the way out.
11. Commit comments (`GET /comments`).
12. Releases. The list object includes `body`.
13. Tags.
14. Annotated tag objects. `GET /git/matching-refs/tags`, then `GET /git/tags/{sha}` when `object.type` is `tag`. A 404 on matching-refs means there are no tag refs and the pull continues.
15. Issue events (`GET /issues/events`).
16. Contributors.

Issues and pull requests are full GETs. The other rows are the list objects for those routes, because a second GET would store the same comment, review, commit, or release again.

List pages inside one collection stay in order, 100 per page, and stop with an error after 200 pages (20,000 objects) instead of truncating quietly. Independent lists run together. The detail wave (issue GETs, pull GETs, reviews, per-branch commits, pull-request commits, annotated tags) runs together, then the JSONL is written in the order above. Tune in-flight requests with `--concurrency N` or `GITHUB_PULL_CONCURRENCY` (default 32, allowed 1–80).

Every worker shares one gate (EMBER-44). It pauses when `x-ratelimit-remaining` (or `RateLimit-Remaining`) is at or under 50, or under a tenth of a smaller limit, until `x-ratelimit-reset`. A 403/429 waits out `Retry-After` or that reset, up to an hour, because GitHub's primary window is an hour and the docs say not to call again before the reset. A secondary limit with primary quota still left waits 60 seconds, then 120. A 429 with no server delay, a 5xx, or a dropped connection backs off 1s, 2s, 4s, … with jitter, each sleep at most 120 seconds. Five tries, then the pull stops. A permission 403 is not retried. Details and the PAT versus installation-token budgets: [`docs/ingestion/github.md`](../../docs/ingestion/github.md).

### What this cut does not pull

These are intentional. The pull stays on Metadata, Contents, Issues, and Pull requests read, and it avoids per-object walks that would multiply the rate limit by commit or comment count.

- File trees, blobs, `/contents`, and pull request file diffs.
- `GET /commits/{sha}` (the `files` list). The commit list object, including `commit.message`, is stored.
- Check runs and commit statuses. REST has no repo-wide list, and Checks is outside this permission set. Subscribe to `check_run`, `check_suite`, and `status` for the live path.
- Actions workflows, runs, and jobs (Actions permission). Subscribe to `workflow_run` and `workflow_job` for the live path.
- Deployments, environments, and branch protection. Subscribe to `deployment` and `deployment_status` for the live path.
- Code scanning, Dependabot, and secret scanning alerts.
- Stars, watchers, and forks.
- Reactions on comments and reviews.
- Per-issue timelines (`/issues/{number}/timeline`). Repo-wide issue events are stored instead.
- Discussions and discussion comments. GitHub has no REST collection for them (GraphQL only). Subscribe to `discussion` and `discussion_comment` for the live path.
- Projects v2. No REST collection (GraphQL only).
- Traffic, hooks, teams, and org membership.
- Any repository other than `GITHUB_TEST_REPO`. This command does not replay webhooks.

## Local webhook smoke

This checks signed deliveries against the receiver on your machine. It does not call GitHub and it does not need a public URL. `pull_test.py` is a separate backfill.

```sh
git fetch && git checkout feature/EMBER-35-github-full-backfill

export GITHUB_WEBHOOK_SECRET="throwaway-local-secret"

scripts/smoke_webhook.sh
```

Any throwaway string is fine. The script starts the receiver and POSTs `issues`, `pull_request`, `issue_comment`, `pull_request_review`, `pull_request_review_comment`, `release`, and `create`, each with `X-Hub-Signature-256`. It expects HTTP 202 and one stdout line per delivery. The first two lines are:

```json
{"type":"github","body":{"action":"opened","issue":{"number":12,"title":"Login redirect drops the query string","state":"open","body":"Repro: sign in with ?next=/settings and land on /."},"repository":{"full_name":"acme/widget"},"installation":{"id":1}}}
```

```json
{"type":"github","body":{"action":"opened","pull_request":{"number":3,"title":"Add parser","state":"open","body":"What this changes."},"repository":{"full_name":"acme/widget"},"installation":{"id":1}}}
```

Five more stdout lines follow, one per remaining event, each the posted JSON wrapped unchanged. Success ends with `smoke ok: 7 envelopes` on stderr.

On the cluster, the App's public URL is `https://ember.tail470a31.ts.net` (Tailscale Funnel), and nginx routes `https://ember.tail470a31.ts.net/webhook/github` to Service `github-webhook`. That route currently ends at a placeholder listener that returns 200 and stores nothing (see `infra/k8s/README.md`). This receiver replaces the placeholder behind Service `github-webhook` in EMBER-56; until then, do not leave the App's webhook URL pointing at it, because GitHub would record deliveries as successful while they are thrown away. For a receiver on your own machine, point a tunnel at `GITHUB_INGESTION_PORT` (default `8080`) and set the App webhook URL to `https://<tunnel-host>/webhook/github` with the same secret. Examples: `cloudflared tunnel --url http://127.0.0.1:8080` or `ngrok http 8080`. The tunnel is not part of this smoke and CI does not start one.

## Test

```sh
python3 services/github-ingestion/test_webhook.py
python3 services/github-ingestion/test_pull.py
GITHUB_WEBHOOK_SECRET=ci-local-smoke python3 services/github-ingestion/smoke_webhook.py
```

`test_pull.py` mocks HTTP. CI runs all three. None of them call GitHub.

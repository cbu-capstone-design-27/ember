# services/github-ingestion

GitHub App connector: the local REST backfill (`pull_test.py`) and `enrich:github` (`enrich.py`). Both work in EMBER-39 envelopes, `{"type":"github","body":<raw JSON object>}`. Live webhooks are received and stored by [`services/intake`](../intake/README.md) (EMBER-62).

Jira: EMBER-35. Floor scaffold. The worker is repo- and account-agnostic. It does not clean or sort the payload. Embeddings stay out (EMBER-3). No live tunnel and no App registration in this change.

Rate limits for a GitHub App installation token and for a personal access token, and how the backfill waits them out: [`docs/ingestion/github.md`](../../docs/ingestion/github.md) (EMBER-44).

Installing the App on an account or org defaults to all repositories. Choosing which of those repositories feed Ember is a later webapp concern, not this service.

## Status

`pull_test.py` is a local full REST backfill for one test repository. It is a follow-on to the slim issues / pull requests / default-branch commits cut: one envelope per discrete REST object, not a second copy of the same pull request. `enrich.py` turns GitHub rows in `raw_events` into `source_records` ([Enrich worker](#enrich-worker)).

## GitHub App

This is a GitHub App, not a classic OAuth app and not a single-repo webhook. `ember-ingest` is an example App. The worker does not hard-code its slug, id, or install target.

Permissions below are what `pull_test.py` needs on the installation. Event subscriptions are for live webhooks, which intake receives. The list is in [`services/intake`'s README](../intake/README.md#events).

### Credentials

| Variable | Role |
| --- | --- |
| `GITHUB_APP_ID` | App id. Example: `5075660` (`ember-ingest`). Not a secret. `pull_test.py` and `enrich.py` read it. |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Path to the App `.pem`. Preferred. Never commit the file. |
| `GITHUB_APP_PRIVATE_KEY` | PEM contents, including the BEGIN/END lines. Used by `pull_test.py` and `enrich.py` only when the path is empty; they write it to a temp file to sign with. |
| `GITHUB_TEST_REPO` | `owner/name` for `pull_test.py` only. Example test target: `ryan-stoffel/photon`. Not a filter on the webhook path. |

### Permissions (read)

Repository permissions this cut uses:

- Metadata
- Contents
- Issues
- Pull requests

No organization permissions in this scaffold.

### Events

Subscribe the App to the events in [`services/intake`'s README](../intake/README.md#events). For each one, this is what the REST backfill stores instead:

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

## Local test pull

Example App already created (not hard-coded in the worker):

- Slug: `ember-ingest`
- App ID: `5075660`
- Client ID: `Iv23liDrl4PFQRt0KB4t`
- Org: `cbu-capstone-design-27`
- Installed on the current test target: `ryan-stoffel/photon`

The private key stays on your machine.

After checkout:

```sh
git fetch && git checkout feature/EMBER-35-github-full-backfill

export GITHUB_APP_ID=5075660
export GITHUB_APP_PRIVATE_KEY_PATH="/absolute/path/to/ember-ingest.private-key.pem"
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

## Enrich worker

`enrich.py` is `enrich:github` (EMBER-65). It turns GitHub rows in `raw_events` into `source_records`, fetching only what the payload lacks (`docs/ingestion/github-webhooks.md`), and sets `enriched_at` in the same transaction. It wakes on any notification on `RAW_EVENTS_CHANNEL` (default `raw_events`) and ignores the payload, catches up at start, and polls every 30 s. `CONCURRENCY` (default 4) bounds rows in flight. Ctrl+C or SIGTERM finishes the rows in flight, then exits.

Against the local Postgres, from the repo root:

```sh
docker compose -f infra/docker-compose.yml --env-file .env --profile postgres up -d postgres
docker compose -f infra/docker-compose.yml --env-file .env --profile postgres run --rm postgres-migrate
pip install -r services/github-ingestion/requirements.txt
DATABASE_URL='postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable' \
  python3 services/github-ingestion/enrich.py
```

Only `pull_request` `opened`/`reopened`/`synchronize` and pushes with a full `commits` array call GitHub. Those need `GITHUB_APP_ID` and the App key from [Credentials](#credentials). Rows with `tenant` null wait until the installation is mapped.

The `Dockerfile` builds image `github-ingestion` with `enrich.py` as its command and `openssl` for the App JWT.

`test_enrich.py` runs the worker against a real Postgres with GitHub stubbed. It needs psql and dbmate, like `infra/k8s/apps/postgres/test_migrations.py`:

```sh
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
  python3 services/github-ingestion/test_enrich.py
```

## Test

```sh
python3 services/github-ingestion/test_pull.py
```

`test_pull.py` mocks HTTP and never calls GitHub. CI runs it and `test_enrich.py`. Intake's webhook tests are in [`services/intake`](../intake/README.md#test).

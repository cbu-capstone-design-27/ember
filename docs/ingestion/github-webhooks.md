# GitHub webhooks: event types, enrichment and ordering

- Jira: EMBER-69 (floor). Jira's half is [`jira-webhooks.md`](jira-webhooks.md).
- Status: proposed, for team review. Headers and limits were checked against GitHub's docs (docs.github.com) on 2026-10-13. Links are in [Sources](#sources).
- This is the spec for the `enrich:github` worker (EMBER-65). [`architecture.md`](architecture.md) covers the pipeline as a whole. `services/github-ingestion/README.md` covers the App, its credentials and the backfill.
- Related: EMBER-62 (intake), EMBER-63 (source ordering column), EMBER-35 (GitHub connector)

GitHub sends the data itself (`architecture.md`, [Webhooks per source](architecture.md#webhooks-per-source-data-or-signal)). Intake stores each delivery in `raw_events` as it arrived. `enrich:github` adds only what the payload lacks, then writes `source_records`.

## EMBER-69 acceptance criteria (GitHub)

| Criterion | Where |
|---|---|
| For each subscribed event type, what the payload contains and what the worker fetches | [Event types](#event-types-payload-and-what-enrich-fetches) |
| How `delivery_id` and the source ordering value (EMBER-63) are derived | [`delivery_id`](#delivery_id) and [Ordering value](#ordering-value-ember-63) |
| Known gaps (missed deliveries, out of order, edits and deletes) and how catch-up handles them | [Known gaps and catch-up](#known-gaps-and-catch-up) |
| Lives under `docs/ingestion/` and is linked from `architecture.md` | This file. Linked under [Webhooks per source](architecture.md#webhooks-per-source-data-or-signal) |

## How a delivery arrives

The GitHub App posts JSON to `/webhook/github`. Intake checks `X-Hub-Signature-256` (`sha256=` plus the HMAC-SHA256 of the raw body with the App's webhook secret), stores the body, and answers. GitHub wants a 2xx within 10 seconds, or it records the delivery as failed. There is no handshake.

**Headers:**

| Header | Meaning | Used for |
|---|---|---|
| `X-GitHub-Delivery` | "A globally unique identifier (GUID) to identify the event" (GitHub). A manual redelivery keeps the same value | `delivery_id` |
| `X-GitHub-Event` | The event name, e.g. `pull_request` | `event_name` |
| `X-Hub-Signature-256` | HMAC of the body | Verification |
| `X-GitHub-Hook-ID`, `X-GitHub-Hook-Installation-Target-ID`, `X-GitHub-Hook-Installation-Target-Type` | The App's webhook and the App itself | Not needed. Every App delivery has the same values |

GitHub sends no timestamp header. The only times are inside the payload ([Ordering value](#ordering-value-ember-63)).

**Fields every repository event carries:**

| Field | Meaning | Used for |
|---|---|---|
| `action` | What happened, e.g. `opened`, `edited`, `deleted`. Absent on `push`, `create`, `delete`, `status` | `event_name` suffix, and which row enrich writes |
| `installation.id` | The App installation that saw it | `account_id`, mapped to a tenant through `installations` |
| `repository` | The whole repository object, including `full_name` | The prefix of every `object_id` |
| `sender` | The user who triggered it | Kept in the record |

**`event_name`** for `raw_events`: `X-GitHub-Event`, plus `:<action>` when there is one, e.g. `pull_request:synchronize`.

**Body size:** GitHub doesn't send payloads over 25 MB. Intake refuses bodies over 8 MiB with a 413. Either way the event's snapshot is lost and [reconcile](#known-gaps-and-catch-up) stores the object later.

## Event types: payload and what enrich fetches

The subscription list is `RECOMMENDED_WEBHOOK_EVENTS` in `services/intake/adapters/github.py` (24 names, also in intake's README). Intake doesn't filter on it. **With the App's current permissions (Metadata, Contents, Issues, Pull requests) GitHub only delivers 13 of them**, plus the two installation events every App gets. The rest need permissions the App doesn't have. See [open questions](#open-questions).

| Event | Permission GitHub requires | Delivered today |
|---|---|---|
| `installation`, `installation_repositories` | None. Every App receives them and can't unsubscribe | Yes |
| `repository`, `label` | Metadata | Yes |
| `push`, `create`, `delete`, `commit_comment`, `release` | Contents | Yes |
| `issues`, `issue_comment` | Issues | Yes |
| `milestone` | Issues or Pull requests | Yes |
| `pull_request`, `pull_request_review`, `pull_request_review_comment` | Pull requests | Yes |
| `status` | Commit statuses | **No** |
| `check_run`, `check_suite` | Checks | **No** |
| `workflow_run`, `workflow_job` | Actions | **No** |
| `deployment`, `deployment_status` | Deployments | **No** |
| `discussion`, `discussion_comment` | Discussions | **No** |

**Recommended subscriptions** (EMBER-92). Subscribe the App to the events enrich maps (`MAPPED` in `services/github-ingestion/enrich.py`):

- `issues`, `issue_comment`, `milestone`, `label`
- `pull_request`, `pull_request_review`, `pull_request_review_comment`
- `push`, `create`, `delete`, `commit_comment`, `release`, `repository`
- `installation`, `installation_repositories` (always sent)

Uncheck the CI events (`workflow_*`, `check_*`, `status`) in the App's settings. Enrich marks them enriched in one bulk statement without making a record, so they only add load: 904 of the 981 deliveries one repository sent in 30 minutes were CI events. GitHub has no API for an App's event subscriptions, so this is set on the App's settings page.

**Rules enrich follows:**
- **Fetch only what's missing.** The payload is the record of that moment and is kept as it is. Fetched data is added beside it in `source_records.record`.
- **Every call uses the installation token** (App JWT → installation access token, 1 hour) and **goes through the shared limiter** (`rate_limits`, bucket = installation). Throttling (`Retry-After`, or `x-ratelimit-remaining: 0`) waits and doesn't count as a failure, as in `pull_test.py`.
- **No calls need extra permissions.** Everything below works with the four the App already has.

| API call | Permission | Enrich uses it for |
|---|---|---|
| `GET /repos/{owner}/{repo}/compare/{before}...{after}` | Contents | A push whose `commits` array is full (2048) |
| `GET /repos/{owner}/{repo}/pulls/{number}/files` | Pull requests | The changed files of a pull request (paths, additions, deletions; `patch` when GitHub includes it) |

### Issues, pull requests and their comments

Issues and pull requests share one number space per repository, so `object_id` `<owner>/<repo>#<number>` is unique and matches `architecture.md`'s example. A pull request read by the backfill as an issue resource and as a pull resource folds into the same object.

| Event | Payload carries | Enrich fetches | `source_records` row |
|---|---|---|---|
| `issues` (`opened`, `edited`, `closed`, `reopened`, `labeled`, `assigned`, `milestoned`, `transferred`, …) | The whole `issue` as it stood, with `body`, `labels`, `assignees`, `state`, `updated_at`. `edited` adds `changes` with the old `title` / `body` | Nothing | `issue`, `<repo>#<number>`, version `issue.updated_at` |
| `issues` `deleted` | The issue as it was before the delete | Nothing | `issue`, tombstone, version `deleted:<received_at>` |
| `issue_comment` (`created`, `edited`, `deleted`) | `comment` (`body`, `user`, `created_at`, `updated_at`) and the whole `issue`. A comment on a pull request has `issue.pull_request` set | Nothing. The issue is in the payload. Earlier comments come from reconcile | `issue_comment`, `<repo>/issue-comment/<comment.id>`, version `comment.updated_at`. `deleted` writes a tombstone |
| `pull_request` (`opened`, `edited`, `closed`, `reopened`, `synchronize`, `ready_for_review`, `review_requested`, `labeled`, …) | The whole `pull_request`: `title`, `body`, `state`, `merged`, `head` and `base` refs and SHAs, `updated_at`. No diff and no file list. `synchronize` adds `before` and `after` | On `opened`, `reopened` and `synchronize`: the file list (`pulls/{number}/files`). Other actions: nothing | `pull_request`, `<repo>#<number>`, version `pull_request.updated_at` |
| `pull_request_review` (`submitted`, `edited`, `dismissed`) | `review` (`body`, `state`, `user`, `commit_id`, `submitted_at`) and the `pull_request` | Nothing. The review's line comments arrive as their own `pull_request_review_comment` events | `pull_request_review`, `<repo>/review/<review.id>`, version `review.submitted_at`; `dismissed` uses `<received_at>` |
| `pull_request_review_comment` (`created`, `edited`, `deleted`) | `comment` (`body`, `path`, `line`, `diff_hunk`, `in_reply_to_id`, `updated_at`) and the `pull_request` | Nothing | `pull_request_review_comment`, `<repo>/review-comment/<comment.id>`, version `comment.updated_at`. `deleted` writes a tombstone |

### Code

| Event | Payload carries | Enrich fetches | `source_records` row |
|---|---|---|---|
| `push` | `ref`, `before`, `after`, `forced`, `created`, `deleted`, `pusher`, `head_commit`, and `commits`: up to 2048, each with `id`, `message`, `timestamp`, `author`, `committer`, `distinct`, and `added` / `removed` / `modified` paths. No diffs | The compare API, only when `commits` has 2048 entries | One `commit` per new SHA: `<repo>@<sha>`, version `<sha>` (commits never change). One `ref`: `<repo>:<ref>`, version `after` |
| `create` | `ref`, `ref_type` (`branch` or `tag`), `master_branch` | Nothing | `ref`, `<repo>:<ref>`, version `created:<received_at>` |
| `delete` | `ref`, `ref_type` | Nothing | `ref`, tombstone, version `deleted:<received_at>` |
| `commit_comment` (`created`) | `comment` (`body`, `commit_id`, `path`, `line`, `updated_at`) | Nothing | `commit_comment`, `<repo>/commit-comment/<comment.id>`, version `comment.updated_at` |
| `release` (`published`, `created`, `edited`, `released`, `deleted`, …) | The whole `release`: `tag_name`, `name`, `body`, `draft`, `prerelease`, `created_at`, `published_at` | Nothing | `release`, `<repo>/release/<release.id>`, version `published_at`, else `created_at`; `edited` and `deleted` use `<received_at>` |

### Repository and App state

| Event | Payload carries | Enrich fetches | `source_records` row |
|---|---|---|---|
| `milestone` (`created`, `edited`, `closed`, `opened`, `deleted`) | The whole `milestone` with `title`, `description`, `state`, `due_on`, `updated_at` | Nothing | `milestone`, `<repo>/milestone/<number>`, version `updated_at`. `deleted` writes a tombstone |
| `label` (`created`, `edited`, `deleted`) | `label` (`name`, `color`, `description`). `edited` adds `changes` with the old name | Nothing | `label`, `<repo>/label/<label.id>`, version `<received_at>` (labels have no timestamp) |
| `repository` (`renamed`, `transferred`, `archived`, `unarchived`, `privatized`, `publicized`, `edited`, `deleted`, …) | The whole repository. `renamed` adds `changes.repository.name.from` | Nothing | `repository`, `<repository.id>`, version `<received_at>`. `deleted` writes a tombstone and stops that repository's reconcile |
| `installation` (`created`, `deleted`, `suspend`, `unsuspend`, `new_permissions_accepted`) | `installation` (account, permissions, events) and, on `created`, the `repositories` it can see | Nothing | No record. Updates `installations`. `created` enqueues a reconcile per repository. `deleted` and `suspend` stop them. Stored data is kept (EMBER-53) |
| `installation_repositories` (`added`, `removed`) | `repositories_added`, `repositories_removed` | Nothing | No record. Starts or stops that repository's reconcile |

## `delivery_id`

Intake's key is `unique (source, delivery_id)` (`architecture.md`, [Storage](architecture.md#storage)). A delivery whose `delivery_id` is already stored gets a 2xx and nothing else.

| Body | `delivery_id` | Why |
|---|---|---|
| Any webhook | `X-GitHub-Delivery` | GitHub calls it globally unique, so no installation prefix is needed. GitHub never retries on its own. A manual or API redelivery reuses the original value, so a redelivery of something already stored is a no-op |
| Reconcile or backfill | `<object id>@<version>`, as `architecture.md` defines | An issue or pull request: `<repo>#<number>@<updated_at>`. A comment: `<repo>/issue-comment/<id>@<updated_at>`. A commit: `<repo>@<sha>` |

A pull request read by both paths gets two `raw_events` rows but one `source_records` row, because `unique (source, tenant, object_id, version)` folds the copies together.

## Ordering value (EMBER-63)

GitHub's arrival order isn't its own order. GitHub doesn't document any delivery order, deliveries for one repository are separate requests that can overlap, a redelivery can come days later, and reconcile rows arrive hours later. Workers need the time GitHub says the thing happened. EMBER-63 stores it in `raw_events.occurred_at` ([`architecture.md`](architecture.md#source-ordering-value-ember-63)), set by whoever writes the row. **For GitHub, the value is the first of these that is present:**

1. The main object's `updated_at` (`issue`, `pull_request`, `comment`, `milestone`)
2. A time the event's object has instead: `review.submitted_at`; `release.published_at`, then `created_at`; on `push`, `repository.pushed_at`
3. `raw_events.received_at`

| Body | Gives | Precision |
|---|---|---|
| `issues`, `issue_comment`, `pull_request`, `pull_request_review_comment`, `commit_comment`, `milestone` | `updated_at` | Whole seconds |
| `pull_request_review` (`submitted`) | `review.submitted_at` | Whole seconds |
| `release` (`published`, `created`, `released`, `prereleased`) | `published_at` or `created_at` | Whole seconds |
| `push` | `repository.pushed_at`. In push payloads GitHub sends it as epoch seconds, not an ISO string. Confirm on a real delivery | Whole seconds |
| Every `deleted` action, `dismissed`, `label`, `repository`, `create`, `delete`, `installation*` | `received_at`. A deleted object's `updated_at` is its last edit, not the delete, so it would sort the tombstone before that edit | Microseconds, but it's our clock |
| Every other `pull_request_review` and `release` action, e.g. `edited` | `received_at`. These objects have no `updated_at`, and an edit keeps the original submit or publish time, so it would tie with the event it changed | Microseconds, but it's our clock |
| Reconcile | The object's `updated_at`; a commit's `committer.date` | Whole seconds |

- **Type.** `timestamptz` (EMBER-63). GitHub's ISO-8601 UTC strings and epoch seconds both convert exactly. The same value fills `source_records.occurred_at`.
- **Scope.** `updated_at` is per object, so it orders versions of one issue or comment well. Across objects it is only approximate. Whole seconds tie often (a `labeled` and an `assigned` from one form submit). Ties break on `raw_events.id`.
- **How workers use it.** A record changes an object only if its ordering value is newer than what's stored for that object. A late `edited` can't bring back a deleted comment, and a late `closed` can't undo a newer `reopened`.

## Known gaps and catch-up

Catch-up has two steps, both run by `scheduler:github` and done by `enrich:github`:

1. **Redeliver failed deliveries.** `GET /app/hook/deliveries` (App JWT) lists the last 3 days of deliveries with `guid`, `status_code`, `event`, `installation_id`. For each failed one, `POST /app/hook/deliveries/{id}/attempts`. The redelivery reuses the `guid`, so it lands in intake as the original event, snapshot and all.
2. **Reconcile from REST.** Today's `services/github-ingestion/pull_test.py`, becoming `Enricher.reconcile`, per repository from `cursors` (streams like `repo:<owner>/<repo>:issues`). Issues and pull requests via `GET /repos/{owner}/{repo}/issues?since=<cursor>&state=all`, comments via `issues/comments?since=`, review comments via `pulls/comments?since=`, then commits and refs. Today's script pulls everything. Reconcile adds the `since` cursor.

| Gap | What happens | Catch-up |
|---|---|---|
| **Intake down, or no 2xx within 10 s** | GitHub marks the delivery failed and **never retries** | Step 1 within 3 days. Step 2 after that, without the event's snapshot |
| **Body over 8 MiB** (intake) or 25 MB (GitHub doesn't send) | Lost. Redelivering an 8 MiB+ body fails again | Step 2 |
| **Pushes GitHub doesn't send**: more than 3 tags or 5000 branches at once | No `push` event at all | Step 2 lists tags and branches |
| **Push over 2048 commits** | `commits` is truncated | Enrich uses the compare API |
| **Edits** | `edited` carries the new object and `changes` with the old values, so each edit is kept | Step 2 sees only the latest version. Edits in between are lost if steps 1 and 2 both missed them |
| **Deletes** (issues, comments, labels, milestones, releases, branches) | The `deleted` action or `delete` event writes a tombstone | Step 2 can't see a delete: the object just isn't listed any more. Step 1 recovers it within 3 days. After that a missed delete stays missed (`architecture.md`'s [failures table](architecture.md#failures-and-replay)) |
| **Out of order** (a comment before its issue's `opened`, `closed` before `edited`) | Each is still written as its own versioned row | The [ordering value](#ordering-value-ember-63) decides which is current |
| **Events the App has no permission for** (`status`, checks, Actions, deployments, discussions) | GitHub doesn't send them | None. See [open questions](#open-questions) |
| **Repository added to or removed from the installation** | `installation_repositories` | Added: reconcile it from the start. Removed: stop. Data is kept (EMBER-53) |
| **Installation not mapped to a tenant** | Stored with `tenant = null`, not processed | `ember-admin map` assigns the tenant and enqueues what was stored |

## Open questions

1. **The subscription list and the permissions disagree.** `RECOMMENDED_WEBHOOK_EVENTS` lists `status`, `check_run`, `check_suite`, `workflow_run`, `workflow_job`, `deployment`, `deployment_status`, `discussion` and `discussion_comment`, but the App has no Commit statuses, Checks, Actions, Deployments or Discussions permission, so GitHub never sends them. Add the read permissions (each installation has to accept them), or drop them from the list?
2. **`architecture.md` says a push lists up to 20 commits.** GitHub's webhook reference says up to 2048. 20 is the limit of the Events API, not webhooks. This doc uses 2048. `architecture.md` needs a one-line fix.
3. **Pull request files:** fetch the list on `opened`, `reopened` and `synchronize` as proposed, and keep `patch`? It is the only per-event call, and `pull_test.py` doesn't fetch files today.
4. **Redelivery from enrich:** step 1 needs the App JWT, which enrich holds already (`architecture.md`, [Per-source auth](architecture.md#per-source-auth)). Confirm `scheduler:github` runs it before each reconcile.

## Sources

Checked on 2026-10-13:
- [Webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads): delivery headers (`X-GitHub-Delivery` "a globally unique identifier (GUID) to identify the event"), the 25 MB cap, the permission each event needs, `installation` and `installation_repositories` sent to every App, `push` limits (2048 commits; none for more than 3 tags or 5000 branches)
- [Best practices for using webhooks](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks): the 10-second response, "If you request a redelivery, the `X-GitHub-Delivery` header will be the same as in the original delivery", check event and `action`
- [Handling failed webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries): "GitHub does not automatically redeliver failed deliveries"
- [Redelivering webhooks](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks): the 3-day window
- [REST API endpoints for GitHub App webhooks](https://docs.github.com/en/rest/apps/webhooks): `GET /app/hook/deliveries`, `POST /app/hook/deliveries/{delivery_id}/attempts`, JWT auth
- `repository.pushed_at` as epoch seconds in `push` payloads: not stated in GitHub's reference. Confirm on the first real delivery
- A sample delivery: `packages/ingestion-envelope/fixtures/github.json` (`issues` `opened`)

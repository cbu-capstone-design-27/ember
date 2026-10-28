# GitLab webhooks: event types, enrichment and ordering

- Jira: EMBER-71 (drop-first), epic EMBER-20 (GitLab Ingestion).
- Status: proposed, for team review. Headers, payloads and API endpoints were checked against GitLab's docs (docs.gitlab.com) on 2026-10-14. Links are in [Sources](#sources).
- This is the spec for the `enrich:gitlab` worker (EMBER-68). [`architecture.md`](architecture.md) covers the pipeline as a whole. [`gitlab.md`](gitlab.md) covers auth, rate limits and the connector in `services/gitlab-ingestion`.
- Related: EMBER-62 (intake), EMBER-63 (source ordering column), EMBER-54 and EMBER-55 (GitLab connector and rate limits), EMBER-47 (test project)

GitLab sends the data itself (`architecture.md`, [Webhooks per source](architecture.md#webhooks-per-source-data-or-signal)). Intake stores each delivery in `raw_events` as it arrived. `enrich:gitlab` adds only what the payload lacks, then writes `source_records`.

## EMBER-71 acceptance criteria

| Criterion | Where |
|---|---|
| For each subscribed webhook type, what the payload contains and what the worker fetches from the API | [Event types](#event-types-payload-and-what-enrich-fetches) |
| How `delivery_id` and the source ordering value (EMBER-63) are derived | [`delivery_id`](#delivery_id) and [Ordering value](#ordering-value-ember-63) |
| Known gaps (missed deliveries, out-of-order delivery, edits and deletes) and how catch-up handles them | [Known gaps and catch-up](#known-gaps-and-catch-up) |
| Lives under `docs/ingestion/` and is linked from `architecture.md` | This file. Linked under [Webhooks per source](architecture.md#webhooks-per-source-data-or-signal) |

## How a delivery arrives

A project webhook posts JSON to `/webhook/gitlab`. Intake checks the signing token (`webhook-signature`, an HMAC-SHA256 of `<webhook-id>.<webhook-timestamp>.<body>`) or the legacy secret token (`X-Gitlab-Token`), stores the body, and answers. There is no handshake. Intake's `gitlab` adapter (`services/intake/adapters/gitlab.py`, EMBER-83) does this, with the checks `services/gitlab-ingestion/receiver.py` makes.

GitLab sends deliveries "asynchronously from Sidekiq nodes" and documents no order. It wants a 200 or 201 within 10 seconds on GitLab.com. A 4xx, a 5xx or a timeout is a failure. After 4 failures in a row the webhook is temporarily disabled, for 1 minute at first and up to 24 hours. After 40 it is disabled for good, until someone re-enables it. GitLab says to "prepare for duplicate events if a webhook times out".

**Headers:**

| Header | Meaning | Used for |
|---|---|---|
| `webhook-id` | A message ID that stays the same across retries and a manual Resend. Equal to `Idempotency-Key` | `delivery_id` |
| `Idempotency-Key` | The same value, under its older name | `delivery_id` when `webhook-id` is missing |
| `X-Gitlab-Event` | `<Event> Hook`, e.g. `Merge Request Hook`. Issues and work items both send `Issue Hook` | Logged. `event_name` comes from the body instead |
| `X-Gitlab-Event-UUID` | Unique for a non-recursive webhook. A webhook triggered by an earlier webhook shares that one's value | Logged only. Not a `delivery_id` (see [`delivery_id`](#delivery_id)) |
| `X-Gitlab-Instance` | The GitLab instance that sent it | Its host is the first half of `account_id`. Intake accepts a bare hostname or a URL (`https://gitlab.com`), so either form gives `gitlab.com`. Confirm on a real delivery |
| `X-Gitlab-Webhook-UUID` | The webhook itself | Not needed |
| `webhook-timestamp` | Unix seconds when GitLab built the request | Verification (replay window) only. It is not when the change happened |
| `webhook-signature`, `X-Gitlab-Token` | Signing token signature, or the legacy plain-text secret | Verification |

**Fields every subscribed body carries:**

| Field | Meaning | Used for |
|---|---|---|
| `object_kind` | `merge_request`, `issue`, `work_item`, `note` or `push` | `event_name`, and which row enrich writes |
| `object_attributes.action` | What happened, e.g. `open`, `update`, `merge`, `create`. Absent on `push` | `event_name` suffix |
| `project` | `id`, `path_with_namespace`, `default_branch`, `web_url` | `project.id` prefixes every `object_id`. The top-level group in `path_with_namespace` is the second half of `account_id` |
| `user` (`user_username` and friends on `push`) | Who triggered it | Kept in the record |
| `changes` | `previous` and `current` for each changed field on merge request and issue updates. GitLab warns it can be empty | Kept in the record |

- **`event_name`** for `raw_events`: `object_kind`, plus `:<action>` when there is one, e.g. `merge_request:update`, `note:create`, `work_item:close`, `push`. The body is used instead of `X-Gitlab-Event` because one header, `Issue Hook`, covers both issues and work items.
- **`account_id`**: `<X-Gitlab-Instance>/<top-level group>`, e.g. `gitlab.com/gitlab-org`. One read token per tenant group ([`gitlab.md`, Auth](gitlab.md#auth)), so this also names the token.
- **Times:** since GitLab 19.0, payload times are ISO 8601 with milliseconds and `Z`, e.g. `2026-01-16T05:56:25.000Z`, the same as the REST API. Older self-managed instances send `2026-01-16 05:56:25 UTC`. Enrich parses both.
- **Body size:** GitLab.com sends up to 25 MB. Intake refuses bodies over 8 MiB with a 413, which GitLab counts as a failure (see [open questions](#open-questions)). Either way the event's snapshot is lost and [reconcile](#known-gaps-and-catch-up) stores the object later.

## Event types: payload and what enrich fetches

The webhook's triggers are `RECOMMENDED_WEBHOOK_TRIGGERS` in `services/gitlab-ingestion/receiver.py`. Intake doesn't filter on them. EMBER-68's acceptance criteria include push events, which the first cut left off, so this doc proposes adding **Push events**.

| Trigger | `X-Gitlab-Event` | `object_kind` | Status |
|---|---|---|---|
| Merge request events | `Merge Request Hook` | `merge_request` | Subscribed |
| Issues events | `Issue Hook` | `issue` for issues and Service Desk issues; `work_item` for tasks, incidents and other work item types | Subscribed |
| Comments | `Note Hook` | `note` | Subscribed |
| Push events | `Push Hook` | `push` | **Proposed** for EMBER-68. Add to `RECOMMENDED_WEBHOOK_TRIGGERS` and the service README |
| Confidential issues, Confidential comments | `Issue Hook`, `Note Hook` | `issue`, `note` (`event_type` `confidential_issue`, `confidential_note`) | Off, as in [`gitlab.md`](gitlab.md#webhooks) |
| Tag push, Emoji, Milestone, Release, Wiki page, Pipeline, Job, Deployment, Feature flag | `Tag Push Hook`, `Emoji Hook`, … | `tag_push`, `emoji`, … | Off. Deferred, as in [`gitlab.md`](gitlab.md#webhooks) |

**Rules enrich follows:**
- **Fetch only what's missing.** The payload is the record of that moment and is kept as it is. Fetched data is added beside it. `source_records.record` has the same shape as `enrich:github`'s: `{"payload": ..., "fetched": ..., "tombstone": true}`, with `fetched` and `tombstone` only when they apply.
- **Every call uses the tenant's `read_api` token** (`PRIVATE-TOKEN`) and **goes through the shared limiter** (`rate_limits`, bucket = `account_id`, i.e. one bucket per token). Throttling works as in `pull.py`: when `RateLimit-Remaining` drops to 50 or fewer, wait until `RateLimit-Reset`. A 429 waits out `Retry-After`. Neither counts as a failure.
- **`read_api` is enough** for every call below.
- **Confidential and internal objects make no record.** Enrich drops an issue with `confidential: true` and a note with `internal: true`, as `pull.py` does, in case a webhook is set up with the confidential triggers on.

| API call | Enrich uses it for |
|---|---|
| `GET /projects/:id/merge_requests/:iid/diffs` (paged) | The changed files of a merge request: `old_path`, `new_path`, `new_file`, `renamed_file`, `deleted_file`, `diff`. Since GitLab 18.4, `collapsed` and `too_large` mark diffs GitLab leaves out. Replaces `.../changes`, deprecated in 15.7 and removed in API v5 |
| `GET /projects/:id/repository/compare?from=<sha>&to=<sha>` | The full commit list of a push with more than 20 commits. GitLab says `commits` is "always complete, even when `compare_timeout` is `true`". Enrich keeps `commits` and drops `diffs` |

### Merge requests, issues and comments

Merge requests and issues have **separate** `iid` sequences in a project, so `!5` and `#5` are different objects. `object_id` uses GitLab's own reference marks: `!` for a merge request, `#` for an issue. It uses `project.id`, not the path, so renaming or moving a project doesn't split an object's history. The path is still in the record. Issues and project work items (tasks, incidents) share one `iid` sequence and one kind.

| Event | Payload carries | Enrich fetches | `source_records` row |
|---|---|---|---|
| `merge_request` `open`, `reopen`, and `update` with `oldrev` | The whole merge request as it stood in `object_attributes`: `title`, `description`, `state`, `draft`, `source_branch`, `target_branch`, `last_commit`, `created_at`, `updated_at`. `oldrev` is present only when the update added commits. Top-level `labels`, `assignees`, `reviewers` and `changes`. **No diff and no file list** | The diffs (`merge_requests/:iid/diffs`) | `merge_request`, `<project.id>!<iid>`, version `updated_at` |
| `merge_request` `update` without `oldrev`, `close`, `merge` | The same | Nothing | `merge_request`, `<project.id>!<iid>`, version `updated_at` |
| `merge_request` `approval`, `unapproval`, `approved`, `unapproved` | The same, plus `user`, the approver | Nothing | `merge_request`, `<project.id>!<iid>`, version `<action>:<received_at>`. GitLab doesn't say these bump `updated_at`. If one didn't, a version of `updated_at` would fold the approval into the previous row. Confirm on a real delivery |
| `issue` or `work_item` `open`, `close`, `reopen`, `update` | The whole issue in `object_attributes`: `title`, `description`, `state`, `type`, `labels`, `due_date`, `confidential`, `created_at`, `updated_at`, `last_edited_at`. Top-level `assignees`, `labels` and `changes` | Nothing | `issue`, `<project.id>#<iid>`, version `updated_at` |
| `note` `create`, `update` | `object_attributes`: `note` (the text), `noteable_type` (`MergeRequest`, `Issue`, `Commit`, `Snippet`), `noteable_id`, `author_id`, `internal`, `created_at`, `updated_at`, and `line_code` on a diff comment. Plus the parent (`merge_request`, `issue`, `commit` or `snippet`) as it stood | Nothing. The parent is in the payload. Earlier comments come from reconcile | `note`, `<project.id>/note/<id>`, version `updated_at` |

### Code

| Event | Payload carries | Enrich fetches | `source_records` row |
|---|---|---|---|
| `push` | `ref`, `before`, `after`, `checkout_sha`, `user_*`, `total_commits_count`, and `commits`: **the newest 20 only**, each with `id`, `message`, `title`, `timestamp`, `author`, and `added` / `modified` / `removed` paths. No diffs | When `total_commits_count` > 20: the compare API from `before` to `after`. A new branch (`before` is 40 zeros) compares from `project.default_branch` instead. The first push to an empty project keeps the payload's 20 and leaves the rest to reconcile | One `commit` per SHA: `<project.id>@<sha>`, version `<sha>` (commits never change). One `ref`: `<project.id>:<ref>`, version `after` |
| `push` deleting a branch (`after` is 40 zeros, `commits` empty) | `ref`, `before`, `after` | Nothing | `ref`, tombstone, version `deleted:<received_at>`. Confirm the zeros on a real delivery |

## `delivery_id`

Intake's key is `unique (source, delivery_id)` (`architecture.md`, [Storage](architecture.md#storage)). A delivery whose `delivery_id` is already stored gets a 2xx and nothing else.

| Body | `delivery_id` | Why |
|---|---|---|
| Any webhook | `webhook-id`, else `Idempotency-Key` (the same value) | GitLab keeps it "consistent across webhook retries", and Resend sends "the same data with the same `Idempotency-Key`". A retry or a resend of something already stored is a no-op. `receiver.py` already de-duplicates on it |
| Reconcile or backfill | `<object id>@<version>`, as `architecture.md` defines | A merge request: `<project.id>!<iid>@<updated_at>`. An issue: `<project.id>#<iid>@<updated_at>`. A note: `<project.id>/note/<id>@<updated_at>`. A commit: `<project.id>@<sha>`. The project: `<project.id>@<last_activity_at>` |

- **Not `X-Gitlab-Event-UUID`.** A webhook set off by an earlier webhook shares that one's value, so two different deliveries can carry it. GitLab also doesn't say it survives a retry. [`architecture.md`'s adapter table](architecture.md#source-adapters) uses `webhook-id` for this reason.
- **Versions are normalized.** Enrich writes every time it uses as a version as UTC ISO 8601 with milliseconds (`2026-01-16T05:56:25.000Z`). That way a webhook copy and a REST copy of one version fold into one `source_records` row through `unique (source, tenant, object_id, version)`, whichever time format the instance sends.

## Ordering value (EMBER-63)

GitLab's arrival order isn't its own order. Sidekiq sends deliveries in parallel with no documented order, a resend can come days later, and reconcile rows arrive hours later. Workers need the time GitLab says the thing happened. EMBER-63 stores it in `raw_events.occurred_at` ([`architecture.md`](architecture.md#source-ordering-value-ember-63)), set by whoever writes the row. **For GitLab, the value is the first of these that applies:**

1. `raw_events.received_at` for a branch delete, a push, and the four approval actions. A deleted branch has no time of its own. A push carries no push time, only commit times, which the author controls. GitLab doesn't say approvals bump `updated_at`.
2. Otherwise `object_attributes.updated_at` (`merge_request`, `issue`, `work_item`, `note`).
3. `raw_events.received_at` if that's missing.

| Body | Gives | Precision |
|---|---|---|
| `merge_request`, `issue`, `work_item`, `note` | `object_attributes.updated_at` | Milliseconds since GitLab 19.0. Whole seconds in the older `… UTC` format |
| `merge_request` `approval`, `unapproval`, `approved`, `unapproved` | `received_at` | Microseconds, but it's our clock |
| `push`, including a branch delete | `received_at` | Microseconds, but it's our clock |
| Reconcile | The object's `updated_at`; a commit's `committed_date`; the project's `last_activity_at` | Milliseconds |

- **Type.** `timestamptz` (EMBER-63), as for every source. Both of GitLab's time formats convert exactly. The same value fills `source_records.occurred_at`.
- **Scope.** `updated_at` is per object, so it orders versions of one merge request, issue or note well. Across objects it is only approximate. Ties break on `raw_events.id`.
- **Pushes.** A ref's own history gives the true order: a push whose `before` isn't the stored ref's `after` arrived out of order or followed a missed push.
- **How workers use it.** A record changes an object only if its ordering value is newer than what's stored for that object. A late `update` can't undo a newer `close`, and a late `reopen` can't undo a newer `merge`.

## Known gaps and catch-up

Catch-up has two steps, both run by `scheduler:gitlab` and done by `enrich:gitlab`:

1. **Resend failed deliveries.** `GET /projects/:id/hooks/:hook_id/events?status=client_failure` (and `server_failure`) lists the last 7 days of deliveries. `POST /projects/:id/hooks/:hook_id/events/:hook_event_id/resend` sends one again, with the same `Idempotency-Key`, so it lands in intake as the original event, snapshot and all. GitLab allows 5 resends a minute per user per project. Both calls need the Maintainer or Owner role (see [open questions](#open-questions)).
2. **Reconcile from REST.** Today's `services/gitlab-ingestion/pull.py --updated-after`, becoming `Enricher.reconcile`, per project from `cursors` (stream `project:<id>`). It lists merge requests and issues updated since the cursor, with all their notes. A new note bumps its parent's `updated_at`, so new comments are covered. It doesn't list commits or branches yet.

| Gap | What happens | Catch-up |
|---|---|---|
| **Intake down, or no 2xx within 10 s** | GitLab counts a failure. After 4 in a row the webhook is temporarily disabled (1 minute, growing to 24 hours), after 40 permanently. Nothing is delivered while it's disabled | Step 1 within 7 days. Step 2 after that, without the events' snapshots. A permanently disabled webhook stays off until someone re-enables it, so onboarding (CLI, web app) should show its state |
| **Timeout after intake stored it** | GitLab may send it again | Same `Idempotency-Key`, so `unique (source, delivery_id)` makes it a no-op |
| **Body over 8 MiB** (intake) or 25 MB (GitLab.com) | Lost. Intake's 413 also counts toward the auto-disable | Step 2 |
| **A push that changes more than 3 branches or tags** | "No webhooks are triggered for the entire push event" (`push_event_hooks_limit`, 3 by default) | None today. Step 2 needs to list commits and branches (`repository/commits?since=`, `repository/branches`). Follow-up for `Enricher.reconcile` |
| **Push over 20 commits** | `commits` holds the newest 20 | Enrich uses the compare API |
| **Edits** | Merge request and issue `update` carries the new object and `changes` with the old values, so each edit is kept. A note `update` carries the new text only | Step 2 sees only the latest version. Edits in between are lost if steps 1 and 2 both missed them |
| **Deletes** (issues, merge requests, notes) | **GitLab sends no webhook for them** | Neither step can see a delete: the object just isn't listed any more. It stays current in the graph (`architecture.md`'s [failures table](architecture.md#failures-and-replay)). See [open questions](#open-questions) |
| **Branch deleted** | A `push` with `after` all zeros writes a tombstone | Step 1 within 7 days. Step 2 can't see it |
| **Out of order** (a note before its merge request's `open`, `close` before `update`) | Each is still written as its own versioned row | The [ordering value](#ordering-value-ember-63) decides which is current |
| **Confidential issues and internal notes** | Not delivered with these triggers. Reconcile skips them too | None, by design ([`gitlab.md`](gitlab.md#webhooks)) |
| **Group-level work items (epics)** | Only group webhooks send them, which need Premium or Ultimate | None. Out of scope for project webhooks |
| **Project renamed or moved** | `path_with_namespace` changes | Nothing to do. `object_id` uses `project.id` |
| **Group not mapped to a tenant** | Stored with `tenant = null`, not processed | `ember-admin map` assigns the tenant and enqueues what was stored |
| **Times from an instance older than 19.0** | `YYYY-MM-DD HH:MM:SS UTC` instead of ISO 8601 | Enrich parses both and normalizes versions |

## Open questions

1. **Push events:** add `Push events` to `RECOMMENDED_WEBHOOK_TRIGGERS` for EMBER-68, as proposed? Tag push stays off.
2. **Resending needs more than `read_api`.** Listing and resending webhook events needs the Maintainer or Owner role, and the resend is a write. Give `scheduler:gitlab` a second, stronger token, or rely on reconcile alone and accept losing the snapshots?
3. **Merge request diffs:** keep the `diff` text, or only paths and flags? It is the only per-event call. `too_large` already bounds it.
4. **413 and auto-disable (EMBER-62):** GitLab counts a 413 as a failure, and 4 in a row disable the webhook. Should intake answer GitLab's oversized bodies with a 200 and log them for reconcile instead?
5. **Deletes:** accept losing them, as `architecture.md` does, or have reconcile occasionally list every `iid` and tombstone the missing ones?
6. **Live webhook test:** the EMBER-47 test project (`gitlab-org/ruby/gems/gitlab-dangerfiles`, PR #29) isn't team-maintained, so it can't take a webhook. EMBER-68's live check needs a project the team maintains.

## Sources

Checked on 2026-10-14:
- [Webhook events](https://docs.gitlab.com/user/project/integrations/webhook_events/): event types, `X-Gitlab-Event` names and `object_kind`, payload fields, `action` values, `oldrev`, `changes` "can be empty", "the newest 20 commits only" and `total_commits_count`, work items under `Issue Hook`, comment events "made or edited", the ISO 8601 time format since GitLab 19.0, redacted emails
- [Webhooks](https://docs.gitlab.com/user/project/integrations/webhooks/): delivery headers (`webhook-id` and `Idempotency-Key` consistent across retries, `X-Gitlab-Event-UUID` shared by recursive webhooks), auto-disable after 4 and 40 failures, `push_event_hooks_limit` of 3 branches or tags, Resend with the same `Idempotency-Key`, "asynchronously from Sidekiq nodes", duplicates after a timeout, group webhooks on Premium and Ultimate
- [Project webhooks API](https://docs.gitlab.com/api/project_webhooks/): listing a webhook's events (past 7 days, status filter), resending one (5 a minute), Maintainer or Owner role
- [Merge requests API](https://docs.gitlab.com/api/merge_requests/): `GET .../diffs` and its fields, `collapsed` and `too_large` (18.4), `.../changes` deprecated in 15.7
- [Repositories API](https://docs.gitlab.com/api/repositories/): `GET /projects/:id/repository/compare`, `commits` "always complete"
- Sample delivery: `packages/ingestion-envelope/fixtures/gitlab.json` (`merge_request` `open`)
- 10-second timeout, 25 MB payloads and rate limits: [`gitlab.md`](gitlab.md#rate-limits), checked on 2026-10-01

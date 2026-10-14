# Jira webhooks: event types, enrichment and ordering

- Jira: EMBER-69 (floor). GitHub's half is [`github-webhooks.md`](github-webhooks.md).
- Status: proposed, for team review. Headers, retries and limits were checked against Atlassian's docs (developer.atlassian.com) on 2026-10-13. Links are in [Sources](#sources).
- This is the spec for the `enrich:jira` worker (EMBER-66). [`architecture.md`](architecture.md) covers the pipeline as a whole. `services/jira-ingestion/README.md` covers webhook registration, credentials and the backfill.
- Related: EMBER-62 (intake), EMBER-63 (source ordering column), EMBER-31 (Jira connector)

Jira sends the data itself (`architecture.md`, [Webhooks per source](architecture.md#webhooks-per-source-data-or-signal)). Intake stores each delivery in `raw_events` as it arrived. `enrich:jira` adds only what the payload lacks, then writes `source_records`.

## EMBER-69 acceptance criteria (Jira)

| Criterion | Where |
|---|---|
| For each subscribed event type, what the payload contains and what the worker fetches | [Event types](#event-types-payload-and-what-enrich-fetches) |
| How `delivery_id` and the source ordering value (EMBER-63) are derived | [`delivery_id`](#delivery_id) and [Ordering value](#ordering-value-ember-63) |
| Known gaps (missed deliveries, out of order, edits and deletes) and how catch-up handles them | [Known gaps and catch-up](#known-gaps-and-catch-up) |
| Lives under `docs/ingestion/` and is linked from `architecture.md` | This file. Linked under [Webhooks per source](architecture.md#webhooks-per-source-data-or-signal) |

## How a delivery arrives

Ember uses a Jira **admin webhook**, one per site (Jira Administration → System → Webhooks). Forge, Connect and OAuth 2.0 dynamic webhooks are a different path and aren't implemented. Jira posts JSON to `/webhook/jira`. Intake checks `X-Hub-Signature` (`sha256=` plus the HMAC-SHA256 of the raw body with the webhook's secret), stores the body, and answers. There is no handshake.

**Intake must answer exactly 200.** Atlassian: "All other response codes should be treated as an unsuccessful attempt." Today's `services/jira-ingestion/receiver.py` answers 202. Intake as `architecture.md` describes it answers 200.

**Headers:**

| Header | Meaning | Used for |
|---|---|---|
| `X-Atlassian-Webhook-Identifier` | "Unique within a Jira Cloud tenant and is the same across retries" (Atlassian) | `delivery_id`, with the site ([below](#delivery_id)) |
| `X-Atlassian-Webhook-Retry` | The retry count, present only on a retry | Logged. Not needed, because the identifier repeats |
| `X-Atlassian-Webhook-Flow` | `Primary` or `Secondary`. Primary deliveries arrive within 30 s, Secondary within 15 minutes | Logged. Explains late arrivals ([Ordering value](#ordering-value-ember-63)) |
| `X-Hub-Signature` | HMAC of the body | Verification |

**Top-level body fields:**

| Field | Meaning | Used for |
|---|---|---|
| `webhookEvent` | The event, e.g. `jira:issue_updated`. Present on every delivery | `event_name` |
| `timestamp` | When the event happened, epoch milliseconds. Present on every delivery | Last-resort ordering value |
| `issue_event_type_name` | The Jira event behind an issue webhook, e.g. `issue_generic`, `issue_commented` | Kept in the record |
| `user` | Who did it. "Always present … for issue events" (Atlassian) | Kept in the record |
| `issue`, `changelog`, `comment`, `worklog`, … | The entity, per event below | Everything below |
| `cloudId` | The site's id. Admin webhook bodies often omit it (`services/jira-ingestion/README.md`) | Not relied on |

**`event_name`** for `raw_events`: `webhookEvent` as sent, e.g. `comment_created`.

**Body size:** Jira doesn't deliver webhooks over 25 MB. Intake refuses bodies over 8 MiB with a 413, which Jira doesn't retry. Reconcile stores the issue later.

## Event types: payload and what enrich fetches

The subscription list is in `services/jira-ingestion/README.md` (registration step 4). Unlike GitHub there is no list in code: `receiver.py` accepts any JSON object and doesn't read `webhookEvent` except to log it. The webhook's JQL is empty, so every project on the site is sent.

Atlassian's webhook page documents only `issue`, `user`, `changelog` and `comment`. The other payload shapes below follow Jira's REST objects of the same name and must be confirmed on a real delivery before EMBER-66 relies on them ([open questions](#open-questions)).

**Rules enrich follows:**
- **Fetch only what's missing.** The payload is the record of that moment and is kept as it is. Fetched data is added beside it in `source_records.record`.
- **Every call uses basic auth** (account email and API token) and **goes through the shared limiter** (`rate_limits`, bucket = site). `Retry-After` waits and doesn't count as a failure, as in `pull_test.py`.
- **Watchers, votes and remote links have no webhook.** Fetching them on every issue event would be three calls per edit. Reconcile reads them instead.

| API call | Enrich uses it for |
|---|---|
| `GET /rest/api/3/issue/{id}?fields=*all` | An issue a comment, worklog or link points at that isn't stored yet |

### Issues

`object_id` is the issue's numeric `id`, not its `key`, because a key changes when an issue moves project. The key is kept in the record.

| Event | Payload carries | Enrich fetches | `source_records` row |
|---|---|---|---|
| `jira:issue_created` | `issue`: "the same shape returned from the Jira REST API when an issue is retrieved with NO expand parameters": `id`, `key`, `fields` (summary, description as ADF, status, assignee, reporter, labels, priority, `created`, `updated`, custom fields). Attachments added in the create dialog are listed in `fields.attachment` | Nothing | `issue`, `<issue.id>`, version `fields.updated` |
| `jira:issue_updated` | The `issue` after the edit, plus `changelog` = `{id, items: [{field, fieldtype, from, fromString, to, toString}]}` for **this edit only**. There is no separate changelog webhook | Nothing | `issue`, `<issue.id>`, version `fields.updated`. The record keeps `changelog` |
| `jira:issue_deleted` | The `issue` as it was | Nothing | `issue`, tombstone, version `deleted:<timestamp>` |

### Things attached to an issue

| Event | Payload carries | Enrich fetches | `source_records` row |
|---|---|---|---|
| `comment_created`, `comment_updated` | `comment`: `id`, `body` (ADF), `author`, `updateAuthor`, `created`, `updated`, and `self`, which names the issue (`…/issue/<issue id>/comment/<id>`). In practice also a reduced `issue` (`id`, `key`, a few fields) | The issue if it isn't stored | `comment`, `<issue id>/comment/<comment.id>`, version `comment.updated` |
| `comment_deleted` | `comment` as it was | Nothing | `comment`, tombstone, version `deleted:<timestamp>` |
| `worklog_created`, `worklog_updated` | `worklog`: `id`, `issueId`, `author`, `comment`, `started`, `timeSpentSeconds`, `created`, `updated` | The issue if it isn't stored | `worklog`, `<issueId>/worklog/<id>`, version `worklog.updated` |
| `worklog_deleted` | `worklog` as it was | Nothing | `worklog`, tombstone, version `deleted:<timestamp>` |
| `attachment_created` | `attachment` metadata: `id`, `filename`, `author`, `created`, `size`, `mimeType`. Whether it names the issue is unconfirmed | Nothing. File bytes are never fetched (as in `pull_test.py`) | `attachment`, `attachment/<id>`, version `created` |
| `attachment_deleted` | `attachment` as it was | Nothing | `attachment`, tombstone, version `deleted:<timestamp>` |
| `issuelink_created` | `issueLink`: `id`, `sourceIssueId`, `destinationIssueId`, `issueLinkType` (name, inward and outward names) | Either issue if it isn't stored | `issuelink`, `issuelink/<id>`, version `<timestamp>` (links have no timestamp of their own) |
| `issuelink_deleted` | `issueLink` as it was | Nothing | `issuelink`, tombstone, version `deleted:<timestamp>` |
| `issue_property_set` | `property` (`key`, `value`) and the issue it belongs to | Nothing | `issue_property`, `<issue id>/property/<key>`, version `<timestamp>` |
| `issue_property_deleted` | `property` (`key`) | Nothing | `issue_property`, tombstone, version `deleted:<timestamp>` |

Version events (`jira:version_*`) exist but aren't subscribed. Components have no webhook. Both come only from reconcile ([open questions](#open-questions)).

## `delivery_id`

Intake's key is `unique (source, delivery_id)` (`architecture.md`, [Storage](architecture.md#storage)). A delivery whose `delivery_id` is already stored gets a 200 and nothing else.

| Body | `delivery_id` | Why |
|---|---|---|
| Any webhook | `<site>:<X-Atlassian-Webhook-Identifier>` | Atlassian makes the identifier unique only **within one Jira Cloud tenant**, and one intake serves many sites, so the site is the prefix. A retry repeats the identifier, so retries become no-ops |
| Reconcile or backfill | `<object id>@<version>`, as `architecture.md` defines | An issue: `<issue id>@<fields.updated>`. A comment: `<issue id>/comment/<id>@<updated>` |

**`<site>`** is the host of the payload's site, e.g. `ember-capstone.atlassian.net`, and is also `account_id`. Admin bodies often lack `cloudId`, so intake reads the host from the main object's `self` URL (`issue.self`, `comment.self`, `worklog.self`, …). See [open questions](#open-questions).

An issue read by both paths gets two `raw_events` rows but one `source_records` row, because `unique (source, tenant, object_id, version)` folds the copies together.

## Ordering value (EMBER-63)

Jira's arrival order isn't its own order:
- Jira sends up to 20 deliveries at once per site and URL host, so two edits to one issue can arrive swapped.
- A retry comes 5 to 15 minutes after the previous attempt, up to 5 times.
- Secondary-flow deliveries can take up to 15 minutes.
- Reconcile rows arrive hours later.

Workers need the time Jira says the thing happened. EMBER-63 adds a column for it. Its name and type are EMBER-63's call. **For Jira, the value is the first of these that is present:**

1. The entity's own `updated`: `issue.fields.updated`, `comment.updated`, `worklog.updated`
2. `attachment.created`
3. The body's `timestamp`

| Body | Gives | Precision |
|---|---|---|
| `jira:issue_created`, `jira:issue_updated`, `comment_created` / `_updated`, `worklog_created` / `_updated` | `updated`, e.g. `2026-10-13T14:05:11.123-0700` | Milliseconds |
| `attachment_created` | `attachment.created` | Milliseconds |
| Every `_deleted` event, `issuelink_*`, `issue_property_*` | `timestamp`. A deleted entity's `updated` is its last edit, not the delete | Milliseconds |
| Reconcile | The entity's `updated` | Milliseconds |

- **Why `updated` before `timestamp`.** Reconcile has only `updated`. Using the same field on both paths means the webhook copy and the reconcile copy of one edit get the same value.
- **Type.** `timestamptz` is the suggestion for EMBER-63. Jira's ISO strings with an offset and epoch milliseconds both convert exactly. The same value fills `source_records.occurred_at`.
- **Scope.** `updated` is per entity, so it orders versions of one issue or comment well. Across entities it is approximate. Ties break on `changelog.id` when both rows have one (it increases per site), then on `raw_events.id`.
- **How workers use it.** A record changes an object only if its ordering value is newer than what's stored for that object. A late `jira:issue_updated` can't undo a newer one, and a late `comment_updated` can't bring back a deleted comment.

## Known gaps and catch-up

Catch-up is the Jira reconcile (today's `services/jira-ingestion/pull_test.py`, becoming `Enricher.reconcile`). It runs per project from a cursor (`cursors`, stream `project:<id>`). It searches `POST /rest/api/3/search/jql` with `project = <key> AND updated >= "<cursor>" ORDER BY updated ASC`, then reads each issue with its comments, changelog pages, worklogs, remote links, watchers and votes, as the backfill does today. JQL dates have **minute precision** in the account's time zone, so the cursor steps back one minute and the version key folds the overlap.

Jira's "Get failed webhooks" (`GET /rest/api/3/webhook/failed`) would list deliveries that ran out of retries, but "only Connect and OAuth 2.0 apps can use this operation". It doesn't cover an admin webhook, so reconcile is the only catch-up.

| Gap | What happens | Catch-up |
|---|---|---|
| **Jira retries** (intake answered 408, 409, 425, 429 or 5xx, or timed out) | The same identifier arrives again: a 200, nothing stored twice | Nothing needed |
| **Every retry fails** (intake down longer than the 5 retries span, roughly 25 to 75 minutes), or Jira gives up on a slow endpoint | The webhook copy is lost. After 30 minutes of failures Jira makes only one attempt per webhook until one succeeds | Reconcile picks the issue up by `updated` |
| **Body over 8 MiB** (413, not retried) or over 25 MB (not sent) | Lost | Reconcile |
| **Edits to an issue** | Each `jira:issue_updated` carries its own `changelog`, so each edit is kept | **Recoverable.** Reconcile reads the full changelog (`GET /issue/{id}/changelog`), which has every field change with `from`, `to` and `created`. Unlike Slack and GitHub, missed issue edits come back |
| **Edits to a comment or worklog** | `comment_updated` / `worklog_updated` carries the new version | Reconcile sees only the latest. Jira keeps no comment history |
| **Issue and comment deletes** | `_deleted` writes a tombstone | Reconcile can't see a delete: the issue or comment just isn't returned. A missed delete stays missed (`architecture.md`'s [failures table](architecture.md#failures-and-replay)) |
| **Worklog deletes** | `worklog_deleted` writes a tombstone | Recoverable: `GET /rest/api/3/worklog/deleted?since=<ms>` lists deleted worklog ids |
| **Project deleted** | Jira sends **no** `jira:issue_deleted` for its issues (an Atlassian known issue) | None today. Reconcile gets a 404 for the project and should tombstone its issues ([open questions](#open-questions)) |
| **Attachments added in the create dialog** | No `attachment_created` (an Atlassian known issue) | Nothing extra: they are in `jira:issue_created`'s `fields.attachment` |
| **Out of order** (two edits swapped, a comment before its issue, an edit after a delete) | Each is still written as its own versioned row | The [ordering value](#ordering-value-ember-63) decides which is current |
| **Watchers, votes, remote links** | No webhook | Reconcile only. They are as fresh as the reconcile schedule |
| **Site not mapped to a tenant** | Stored with `tenant = null`, not processed | `ember-admin map` assigns the tenant and enqueues what was stored |

## Open questions

1. **EMBER-63 (Jacob):** the column's name and type. This doc suggests `timestamptz`, filled as described in [Ordering value](#ordering-value-ember-63).
2. **Payload shapes Atlassian doesn't document:** `worklog`, `attachment`, `issueLink`, `property`, and the reduced `issue` on comment events. Capture one real delivery of each (the smoke script's bodies are hand-written) and fix this doc before EMBER-66 relies on them. In particular, does `attachment_created` name its issue?
3. **Status code:** today's receiver answers 202, and Atlassian counts anything but 200 as unsuccessful. Intake answers 200. Should the stub receiver change now, before a real webhook is registered?
4. **Site for `delivery_id` and `account_id`:** read from the `self` URL as proposed, or give each site its own path (`/webhook/jira/<site>`)? The second changes `architecture.md`'s HTTP contract.
5. **Version events:** subscribe to `jira:version_*`? The backfill already stores versions, and the README mentions them as optional.
6. **Deleted projects and issues:** worth a periodic check that compares stored issue ids with a search, to tombstone what disappeared without a webhook?

## Sources

Checked on 2026-10-13:
- [Jira Cloud webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/): event names; `webhookEvent` and `timestamp` on every callback; `issue` as the no-expand REST shape; `changelog` only on `jira:issue_updated`; `comment` on comment events; `X-Atlassian-Webhook-Identifier` ("unique within a Jira Cloud tenant and is the same across retries"); `X-Atlassian-Webhook-Retry`; `X-Atlassian-Webhook-Flow` and its 30 s / 15 min delivery targets; retries (up to 5, 5 to 15 minutes apart, on 408, 409, 425, 429, 5xx, timeouts); 20 concurrent deliveries per site and host; "a response code of 200"; known issues (25 MB, project deletion, create-dialog attachments); secure admin webhooks (`X-Hub-Signature`)
- [Jira REST API v3, Webhooks](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-webhooks/): "Get failed webhooks", Connect and OAuth 2.0 apps only, failures kept about 72 hours
- [Jira REST API v3, Issue worklogs](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/): `GET /rest/api/3/worklog/deleted`
- A sample delivery: `packages/ingestion-envelope/fixtures/jira.json` (`jira:issue_updated` with `timestamp` and `changelog`)

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

Jira posts JSON to `/webhook/jira` one of two ways. Intake verifies the delivery, stores the body, and answers. There is no handshake.

- **A dynamic webhook, from Connect Jira (EMBER-131, [ADR 0010](../adr/0010-connecting-sources.md#jira-ember-131)).** An owner consents to Ember's Atlassian OAuth 2.0 (3LO) app and picks a site and projects. The web app registers one webhook on the site with that user's token (`POST /rest/api/3/webhook`), filtered by `project in (<ids>)`. Each delivery carries `Authorization: Bearer <JWT>`, signed HS256 with the app's client secret. The webhook lapses 30 days after it was registered or last extended, so the keep-alive extends it ([below](#keeping-dynamic-webhooks-alive)).
- **An admin webhook, one per site** (Jira Administration → System → Webhooks), for sites set up before Connect Jira. It carries `X-Hub-Signature`: `sha256=` plus the HMAC-SHA256 of the raw body with the webhook's secret. It stays until each site is reconnected through Connect Jira.

`services/intake/README.md`, Jira adapter, has both checks. Every other rule on this page applies to both paths, except where it says otherwise.

**Intake must answer exactly 200.** Atlassian: "All other response codes should be treated as an unsuccessful attempt." Today's `services/jira-ingestion/receiver.py` answers 202. Intake as `architecture.md` describes it answers 200.

**Headers:**

| Header | Meaning | Used for |
|---|---|---|
| `X-Atlassian-Webhook-Identifier` | "Unique within a Jira Cloud tenant and is the same across retries" (Atlassian) | `delivery_id`, with the site ([below](#delivery_id)) |
| `X-Atlassian-Webhook-Retry` | The retry count, present only on a retry | Logged. Not needed, because the identifier repeats |
| `X-Atlassian-Webhook-Flow` | `Primary` or `Secondary`. Primary deliveries arrive within 30 s, Secondary within 15 minutes | Logged. Explains late arrivals ([Ordering value](#ordering-value-ember-63)) |
| `X-Hub-Signature` | HMAC of the body. Admin webhooks only | Verification |
| `Authorization` | `Bearer <JWT>`. Dynamic webhooks only | Verification |

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

**Admin webhook:** the subscription list is in `services/jira-ingestion/README.md` (registration step 4). Intake accepts any JSON object and doesn't read `webhookEvent` except to store it. The webhook's JQL is empty, so every project on the site is sent.

**Dynamic webhook:** `WEBHOOK_EVENTS` in `apps/web/src/lib/jira-connect.ts`. A dynamic webhook can carry only issue, comment, issue property, sprint and version events. So Connect Jira subscribes to the issue, comment and issue property events below, and **worklog, attachment and issue link events never arrive** from it. Each of those changes the issue too (time spent, the attachment list, the links), so a `jira:issue_updated` with that edit in its `changelog` arrives instead, and reconcile reads the worklogs, attachments and links themselves. Sprint and version events are left out, as they are on the admin webhook ([open questions](#open-questions)). The JQL names the projects the owner ticked, by id, because a key changes when a project is renamed. A project created later isn't sent until the owner reconnects and ticks it.

Atlassian's webhook page documents only `issue`, `user`, `changelog` and `comment`. The other payload shapes below follow Jira's REST objects of the same name and must be confirmed on a real delivery before EMBER-66 relies on them ([open questions](#open-questions)).

**Rules enrich follows:**
- **Fetch only what's missing.** The payload is the record of that moment and is kept as it is. Fetched data is added beside it in `source_records.record`.
- **Every call goes through the shared limiter** (`rate_limits`, bucket = site). A site connected through Connect Jira is called at `api.atlassian.com/ex/jira/<cloud id>` with the [`api` credential's](#the-api-credential) bearer token. A site on an admin webhook uses basic auth (account email and API token) on its own host. `Retry-After` waits and doesn't count as a failure, as in `pull_test.py`.
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

**`<site>`** is the host of the payload's site, e.g. `ember-capstone.atlassian.net`, and is also `account_id`. Admin bodies often lack `cloudId`, so intake reads the host from the main object's `self` URL (`issue.self`, `comment.self`, `worklog.self`, …). Atlassian's examples for dynamic webhooks show the same `https://<site>/rest/api/2/issue/<id>` URLs, so both paths derive it the same way. That is unconfirmed until a real dynamic delivery is captured ([open questions](#open-questions)).

An issue read by both paths gets two `raw_events` rows but one `source_records` row, because `unique (source, tenant, object_id, version)` folds the copies together.

## Ordering value (EMBER-63)

Jira's arrival order isn't its own order:
- Jira sends up to 20 deliveries at once per site and URL host, so two edits to one issue can arrive swapped.
- A retry comes 5 to 15 minutes after the previous attempt, up to 5 times.
- Secondary-flow deliveries can take up to 15 minutes.
- Reconcile rows arrive hours later.

Workers need the time Jira says the thing happened. EMBER-63 stores it in `raw_events.occurred_at` ([`architecture.md`](architecture.md#source-ordering-value-ember-63)), set by whoever writes the row. **For Jira, the value is the first of these that is present:**

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
- **Type.** `timestamptz` (EMBER-63). Jira's ISO strings with an offset and epoch milliseconds both convert exactly. The same value fills `source_records.occurred_at`.
- **Scope.** `updated` is per entity, so it orders versions of one issue or comment well. Across entities it is approximate. Ties break on `changelog.id` when both rows have one (it increases per site), then on `raw_events.id`.
- **How workers use it.** A record changes an object only if its ordering value is newer than what's stored for that object. A late `jira:issue_updated` can't undo a newer one, and a late `comment_updated` can't bring back a deleted comment.

## Known gaps and catch-up

Catch-up is the Jira reconcile (today's `services/jira-ingestion/pull_test.py`, becoming `Enricher.reconcile`). It runs per project from a cursor (`cursors`, stream `project:<id>`). It searches `POST /rest/api/3/search/jql` with `project = <key> AND updated >= "<cursor>" ORDER BY updated ASC`, then reads each issue with its comments, changelog pages, worklogs, remote links, watchers and votes, as the backfill does today. JQL dates have **minute precision** in the account's time zone, so the cursor steps back one minute and the version key folds the overlap.

Jira's "Get failed webhooks" (`GET /rest/api/3/webhook/failed`) would list deliveries that ran out of retries, but its reference now says "only Connect apps can use this operation". It covers neither an admin webhook nor a 3LO app's, so reconcile is the only catch-up.

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
| **Site not mapped to a tenant** | Stored with `tenant = null`, not processed | Connect Jira, or `make map-tenant`, assigns the tenant and hands over what was stored |
| **Dynamic webhook lapsed** (the keep-alive didn't extend it within 30 days) | Jira stops sending | Reconnect the site, which registers a new webhook. Reconcile fills the gap |
| **The consenting user lost access to the site** | Jira stops sending, and the token refresh fails | Reconnect as another user. Reconcile fills the gap |
| **Worklog, attachment and issue link events** (dynamic webhook only) | Not offered to dynamic webhooks. The issue's own `jira:issue_updated` arrives | Reconcile |

## The `api` credential

Connect Jira stores one `api` row per site in `source_credentials` (`account_id` = the site's host), sealed as [`packages/source-credentials`](../../packages/source-credentials/README.md) describes. Its plaintext is this JSON object. The web app writes it, and enrich and the keep-alive read and update it:

| Field | Meaning |
|---|---|
| `access_token` | Bearer token for `api.atlassian.com/ex/jira/<cloud_id>`. Lasts about an hour |
| `refresh_token` | Gets a new pair. **Atlassian rotates it**: each refresh returns a new one, and the old one works for only 10 more minutes. Unused for 90 days, it expires |
| `expires_at` | When `access_token` expires, in epoch seconds |
| `scope` | The scopes granted, space-separated |
| `cloud_id` | The site's cloud id, as in `installations.external_id` |
| `webhook_ids` | The dynamic webhooks Ember registered on the site, to extend and to delete |
| `jql` | The webhook's filter, `project in (<ids>)` |
| `atlassian_account_id`, `atlassian_name` | The Atlassian user who consented. The token and the webhooks are theirs |

**Refreshing:** lock the row (`select … for update`), refresh if `expires_at` is less than 5 minutes away, write the new pair back, and commit before using it. Two workers refreshing the same token at once would leave one with a refresh token that no longer works. `freshJiraCredential` in `apps/web/src/lib/jira-connect.ts` and `SiteCredentials.fresh` in `services/jira-ingestion/atlassian.py` do this.

**Who uses it:** enrich calls `api.atlassian.com/ex/jira/<cloud_id>` with `access_token`, but only for rows of the tenant that connected the site, and only while it is connected. Other rows of a site fall back to the API token in `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN`, if that is the site. Both ways share the site's `rate_limits` bucket (`https://<site>`).

## Keeping dynamic webhooks alive

A dynamic webhook lapses 30 days after it was registered or last extended (`PUT /rest/api/3/webhook/refresh`, which moves the expiry to 30 days from now). A refresh token expires after 90 days without use. `services/jira-ingestion/keep_alive.py` runs daily as CronJob `enrich-jira-keep-alive`. For each connected site with an `api` credential, it refreshes the token whether or not it is due, then extends the site's `webhook_ids`.

- **Refresh refused** (`invalid_grant`: the user lost access to the site, revoked the app, or left the token unused for 90 days): it disconnects the site, as Settings' Disconnect does, without the webhook delete that no token can make. Settings then asks for a reconnect. Any other refusal (a wrong client secret, say) is ours to fix, so the site stays connected.
- **Anything else fails** (Atlassian down, a webhook id it no longer knows): the site is logged and the run exits 1, so the Job shows as failed. The next day's run tries again, well inside the 30 days.

## Open questions

1. **Payload shapes Atlassian doesn't document:** `worklog`, `attachment`, `issueLink`, `property`, and the reduced `issue` on comment events. Capture one real delivery of each (the smoke script's bodies are hand-written) and fix this doc before EMBER-66 relies on them. In particular, does `attachment_created` name its issue?
2. **Status code:** today's receiver answers 202, and Atlassian counts anything but 200 as unsuccessful. Intake answers 200. Should the stub receiver change now, before a real webhook is registered?
3. **Site for `delivery_id` and `account_id`:** read from the `self` URL as proposed, or give each site its own path (`/webhook/jira/<site>`)? The second changes `architecture.md`'s HTTP contract. Dynamic deliveries carry the site's host in `self`, as admin ones do: the first one, a `comment_created` on 2026-11-05, was stored for `ember-capstone.atlassian.net`. Its bearer token's claims are `context`, `exp`, `iat`, `iss`, `jti` and `sub`. Still open: does `context` name the site, so intake could refuse a delivery whose `self` names another?
4. **Version events:** subscribe to `jira:version_*`? The backfill already stores versions, and the README mentions them as optional.
5. **Deleted projects and issues:** worth a periodic check that compares stored issue ids with a search, to tombstone what disappeared without a webhook?

## Sources

Checked on 2026-11-04, for dynamic webhooks (EMBER-131):
- [Jira Cloud webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/): the events a dynamic webhook supports; the JQL subset (`issueKey`, `project`, `issuetype`, `status`, `assignee`, `reporter`, `issue.property`, `cf[id]`; `=`, `!=`, `IN`, `NOT IN`); expiry 30 days after registration or refresh; 5 webhooks per app per user on a site for an OAuth 2.0 app; "secured by bearer authentication", the token "signed with the app's client secret"
- [OAuth 2.0 (3LO) apps](https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/): `audience=api.atlassian.com` and `prompt=consent`; accessible-resources lists each site's `id`, `name`, `url` and `scopes`; refresh tokens rotate, with a 10-minute reuse interval and a 90-day inactivity expiry; an expired or reused one gets 403 `invalid_grant`; apps are private until sharing is enabled
- [Jira REST API v3, Webhooks](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-webhooks/) (the OpenAPI spec): register, list, delete and extend, with the `manage:jira-webhook` and `read:jira-work` scopes; "for non-public OAuth apps, webhooks are delivered only if there is a match between the app owner and the user who registered a dynamic webhook"; Get failed webhooks is "only Connect apps"

Checked on 2026-10-13:
- [Jira Cloud webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/): event names; `webhookEvent` and `timestamp` on every callback; `issue` as the no-expand REST shape; `changelog` only on `jira:issue_updated`; `comment` on comment events; `X-Atlassian-Webhook-Identifier` ("unique within a Jira Cloud tenant and is the same across retries"); `X-Atlassian-Webhook-Retry`; `X-Atlassian-Webhook-Flow` and its 30 s / 15 min delivery targets; retries (up to 5, 5 to 15 minutes apart, on 408, 409, 425, 429, 5xx, timeouts); 20 concurrent deliveries per site and host; "a response code of 200"; known issues (25 MB, project deletion, create-dialog attachments); secure admin webhooks (`X-Hub-Signature`)
- [Jira REST API v3, Webhooks](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-webhooks/): "Get failed webhooks", Connect and OAuth 2.0 apps only, failures kept about 72 hours
- [Jira REST API v3, Issue worklogs](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/): `GET /rest/api/3/worklog/deleted`
- A sample delivery: `packages/ingestion-envelope/fixtures/jira.json` (`jira:issue_updated` with `timestamp` and `changelog`)

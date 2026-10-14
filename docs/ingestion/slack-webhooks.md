# Slack webhooks: event types, enrichment and ordering

- Jira: EMBER-70 (floor), epic EMBER-7 (Slack Ingestion)
- Status: proposed, for team review. Payload shapes were checked against Slack's reference (docs.slack.dev) on 2026-10-13. Links are in [Sources](#sources).
- This is the spec for the `enrich:slack` worker (EMBER-67), built in `services/slack-ingestion/enrich.py` for message events and the user and channel cache. [`architecture.md`](architecture.md) covers the pipeline as a whole. [`slack.md`](slack.md) covers the Slack connector: auth, scopes, rate limits, and agent removal.
- Related: EMBER-62 (intake), EMBER-63 (source ordering column), EMBER-36 (Slack connector)

Slack sends the data itself (`architecture.md`, [Webhooks per source](architecture.md#webhooks-per-source-data-or-signal)). Intake stores each delivery in `raw_events` as it arrived. `enrich:slack` adds only what the payload lacks, then writes `source_records`.

## EMBER-70 acceptance criteria

| Criterion | Where |
|---|---|
| For each subscribed event type, what the payload contains and what the worker fetches | [Event types](#event-types-payload-and-what-enrich-fetches) |
| How `delivery_id` and the source ordering value (EMBER-63) are derived | [`delivery_id`](#delivery_id) and [Ordering value](#ordering-value-ember-63) |
| Known gaps (retries, edits and deletes, thread replies) and how catch-up handles them | [Known gaps and catch-up](#known-gaps-and-catch-up) |
| Lives under `docs/ingestion/` and is linked from `architecture.md` | This file. Linked under [Webhooks per source](architecture.md#webhooks-per-source-data-or-signal) |

## How a delivery arrives

Slack posts JSON to `/webhook/slack`. Intake checks the signature (`X-Slack-Signature` over `v0:<X-Slack-Request-Timestamp>:<body>`, 5-minute window), stores the body, and answers within Slack's 3 seconds.

**Bodies intake sees:**

| `type` | What it is | Intake |
|---|---|---|
| `event_callback` | A subscribed event | Stores it (`raw_events`) and enqueues `enrich` |
| `url_verification` | The one-time Request URL check | Answers `{"challenge": …}`. Not stored |
| `app_rate_limited` | Slack is dropping this workspace's events (over 30,000 per workspace per app per hour). One notice per throttled minute | Stores it. Enrich turns it into a reconcile ([gaps](#known-gaps-and-catch-up)) |

**The `event_callback` wrapper** (the inner `event` is per type, below):

| Field | Meaning | Used for |
|---|---|---|
| `team_id` | Workspace the event happened in | `account_id`, mapped to a tenant through `installations` |
| `api_app_id` | Our app | Sanity check |
| `event_id` | "Globally unique across all workspaces" (Slack) | `delivery_id` |
| `event_time` | When Slack dispatched it, whole epoch seconds | Last-resort ordering value |
| `authorizations`, `event_context` | One installation the event is visible to, and a handle to list the rest | Not needed while one bot token serves a workspace |
| `is_ext_shared_channel` | The channel is shared with another organization | Kept in the record. Extract decides what to do with it |
| `event` | The event itself | Everything below |

Inside `event`, `ts` is the item's own timestamp (a message's id within its channel). `event_ts` is when the event happened. Both are strings like `"1717000001.000200"`.

**Headers:** `X-Slack-Retry-Num` (1–3) and `X-Slack-Retry-Reason` (`http_timeout`, `connection_failed`, …) mark a retry. Intake logs them but doesn't need them, because the `event_id` repeats ([`delivery_id`](#delivery_id)).

**`event_name`** for `raw_events`: the inner `event.type`, plus `:<subtype>` when there is one, e.g. `message:message_deleted`. For the notice it's `app_rate_limited`.

## Event types: payload and what enrich fetches

The app subscribes to 18 bot events (`services/slack-ingestion/slack-app-manifest.json`, listed in [`slack.md`](slack.md#events)).

**Rules enrich follows:**
- **Fetch only what's missing.** The payload is the record of that moment and is kept as it is. Fetched data is added beside it in `source_records.record`.
- **People and channels are cached as records** (`kind` `user` and `channel`). Enrich calls `users.info` or `conversations.info` only when the record is missing. `user_change`, `team_join` and the channel events keep the cache current.
- **Every call goes through the shared limiter** (`rate_limits`; bucket = method × workspace), with the existing Slack handling (`slack.md`, [How the connector handles limits](slack.md#how-the-connector-handles-limits)). Throttling waits and doesn't count as a failure.
- **Lookups don't need extra scopes.** Everything below uses the 8 scopes the app already has.

| API call | Rate tier | Scope | Enrich uses it for |
|---|---|---|---|
| `users.info` | Tier 4 (100+/min) | `users:read`, plus `users:read.email` for `profile.email` | A person not yet cached |
| `conversations.info` | Tier 3 (50+/min) | `channels:read` / `groups:read` | A channel not yet cached: name, privacy, topic, purpose, `updated` |
| `conversations.replies` | Tier 3. **Non-Marketplace apps installed since 2025-05-29: 1/min, 15 per page** | `channels:history` / `groups:history` | A reply's parent, or a reacted message that isn't stored |
| `conversations.history` | Same as `replies` | Same | A reacted top-level message that isn't stored (`latest=<ts>`, `inclusive`, `limit=1`) |

### Messages (`message.channels`, `message.groups`)

Both subscriptions deliver `event.type == "message"`: public and private channels respectively. The kind of message is in `subtype` and the thread fields. `object_id` is `<channel>/<ts>`, the same key backfill uses ([`slack.md`, Data shapes](slack.md#data-shapes)).

| Shape (how to tell) | Payload carries | Enrich fetches | `source_records` row |
|---|---|---|---|
| **New message**: no `subtype`, or `bot_message` / `file_share` / `me_message`; no `thread_ts`, or `thread_ts == ts` | `channel`, `channel_type`, `user` (or `bot_id`), `text`, `blocks`, `ts`, `event_ts`; `files` metadata; `thread_ts` on a thread parent | The author and the channel if not cached | `message`, `<channel>/<ts>`, version `ts` |
| **Thread reply**: `thread_ts` present and `!= ts` | The same, plus `thread_ts` (the parent's `ts`) | The parent, if it isn't stored: `conversations.replies` `ts=<thread_ts>`, `limit=1` | `message`, `<channel>/<ts>`, version `ts`; the record keeps `thread_ts` |
| **`thread_broadcast`**: a reply also shown in the channel | The reply, plus `root`: a copy of the parent with `reply_count`, `latest_reply`, `reply_users` | Nothing for the parent (`root` has it) | `message`, `<channel>/<ts>`, version `ts` |
| **`message_replied`**: Slack's notice that a parent got a reply. Hidden. **Arrives without its `subtype` over the Events API** (a bug Slack documents), so detect it by a nested `message` whose `thread_ts == message.ts` | `message` = the parent with `reply_count` and `replies` | Nothing. The reply itself arrives as its own event | Refreshes the parent's thread fields: `<channel>/<message.ts>`, version `event_ts` |
| **`message_changed`**: an edit. Hidden | `ts` = **when it was edited**. `message` = the new message: `message.ts` is the original id, `message.edited` = `{user, ts}`. Slack's reference doesn't show `previous_message`, so treat the old text as optional | Nothing | `message`, `<channel>/<message.ts>`, version `message.edited.ts`, a new row per edit |
| **`message_deleted`**: a delete. Hidden | `ts` = **when it was deleted**, `deleted_ts` = the deleted message's id. `previous_message` is optional as above | Nothing (the message is gone from Slack) | `message`, `<channel>/<deleted_ts>`, version `deleted:<ts>`, a tombstone row |
| **System messages**: `channel_join`, `channel_leave`, `channel_topic`, `channel_purpose`, `channel_name`, and `group_*` versions | The change as a message: `user`, `text`, and e.g. `topic` / `purpose` / `name` | Nothing | `message`, `<channel>/<ts>`, version `ts`. Topic, purpose and name changes also update the channel record. Extract decides what's noise |

"Hidden" messages are history records (edits, deletes, reply notices). Slack never returns them from `conversations.history`, which matters for [catch-up](#known-gaps-and-catch-up).

### Everything else

| Event | Payload carries | Enrich fetches | `source_records` row |
|---|---|---|---|
| `reaction_added`, `reaction_removed` | `user`, `reaction` (emoji name), `item_user`, `item` = `{type: "message", channel, ts}`, `event_ts` | The reacted message only if it isn't stored (`conversations.replies` for a reply, `conversations.history` for a top-level message) | `reaction`, `<channel>/<item.ts>/<reaction>/<user>`, version `event_ts`. `reaction_removed` writes a tombstone |
| `channel_created` | `channel` = `{id, name, created, creator}`. No `event_ts` | `conversations.info` for privacy, topic and purpose | `channel`, `<channel id>`, version `event_time` |
| `channel_rename`, `group_rename` | `channel` = `{id, name, created}`. No `event_ts` | Nothing | `channel`, new name, version `event_time` |
| `channel_archive`, `group_archive` | `channel` (id), `user`. No `event_ts` | Nothing | `channel`, archived, version `event_time` |
| `channel_unarchive`, `group_unarchive` | `channel` (id), `user`. No `event_ts` | Nothing | `channel`, unarchived, version `event_time` |
| `channel_deleted` | `channel` (id). No `event_ts` | Nothing | `channel`, tombstone, version `event_time` |
| `member_joined_channel` | `user`, `channel`, `channel_type`, `team`, `inviter` (blank if they joined on their own). No `event_ts` | The person if not cached. **If `user` is our bot:** enqueue a reconcile of that channel (the bot can read it now) | `membership`, `<channel>/<user>`, version `event_time` |
| `member_left_channel` | `user`, `channel`, `channel_type`, `team`. No `event_ts` | Nothing. **If `user` is our bot:** stop reconciling the channel. Stored data is kept (EMBER-53) | `membership`, tombstone, version `event_time` |
| `user_change` | The whole user object (`profile`; `profile.email` with `users:read.email`; `updated`), `cache_ts`, `event_ts` | Nothing | `user`, `<user id>`, version `user.updated` |
| `team_join` | `user` (Slack's reference elides its fields). No `event_ts` | `users.info` if `user` has no `profile` | `user`, `<user id>`, version `user.updated` |
| `app_uninstalled` | Nothing but `type`. No `event_ts` | Nothing | No record. Marks the workspace disconnected in `installations` and stops its reconcile |
| `tokens_revoked` | `tokens` = `{oauth: [user ids], bot: [user ids]}`. No `event_ts` | Nothing | No record. If our bot's id is listed, same as `app_uninstalled`. Slack doesn't guarantee which of the two arrives first |

## `delivery_id`

Intake's key is `unique (source, delivery_id)` (`architecture.md`, [Storage](architecture.md#storage)). A delivery whose `delivery_id` is already stored gets a 200 and nothing else.

| Body | `delivery_id` | Why |
|---|---|---|
| `event_callback` | `event_id` | Slack says it's globally unique across workspaces, so no `team_id` prefix is needed. A retry is the same event with the same `event_id`, so retries become no-ops. Slack's reference doesn't spell this out. It's how Slack retries in practice ([Sources](#sources)), and `services/slack-ingestion/receiver.py` already relies on it |
| `app_rate_limited` | `rate_limited:<team_id>:<minute_rate_limited>` | It has no `event_id`. Slack sends one per throttled minute, so this is unique |
| `url_verification` | none | Not stored |
| Reconcile or backfill | `<object id>@<version>`, as `architecture.md` defines | A message: `<channel>/<ts>@<edited.ts, or ts if never edited>`. A person: `<user id>@<updated>`. A channel: `<channel id>@<updated>` (`conversations.info`/`list`) |

A message read by both paths (the live event and a reconcile) gets two `raw_events` rows, because their `delivery_id`s differ. It still becomes one `source_records` row: `unique (source, tenant, object_id, version)` folds the copies together.

## Ordering value (EMBER-63)

Slack's arrival order isn't its own order:
- A retry comes 1 or 5 minutes after the original.
- Deliveries for one channel can overlap.
- Reconcile rows arrive hours later.

Workers need the time Slack says the thing happened. EMBER-63 adds a column for it. Its name and type are EMBER-63's call. **For Slack, the value is the first of these that is present:**

1. `event.event_ts`
2. `event.ts`
3. the wrapper's `event_time`

| Body | Gives | Precision |
|---|---|---|
| New messages, replies, broadcasts, system messages, reactions, `user_change`, `message_replied` | `event.event_ts` | Microseconds |
| `message_changed`, `message_deleted` (Slack's examples show no `event_ts`) | `event.ts`: when it was edited or deleted | Microseconds |
| Channel events, `member_*`, `team_join`, `app_uninstalled`, `tokens_revoked` (no `event_ts`) | `event_time` | Whole seconds |
| `app_rate_limited` | `minute_rate_limited` | Minutes |
| Reconcile messages | `edited.ts` if edited, else `ts` | Microseconds |
| Reconcile people and channels | `updated` | Seconds (users), milliseconds (channels) |

- **Type.** A Slack `ts` is epoch seconds with six decimal places, so it converts to a Postgres `timestamptz` exactly, with no rounding. `timestamptz` is the suggestion for EMBER-63. The same value fills `source_records.occurred_at` (and so `episodes.occurred_at`).
- **Scope.** `ts` is unique and ordered **within one channel**. Across channels and event types the value is only approximately ordered, and whole-second `event_time` values can tie. Ties break on `raw_events.id`.
- **How workers use it.** A record changes an object only if its ordering value is newer than what's stored for that object. So a late edit can't bring back a deleted message, and a late `channel_rename` can't undo a newer one.

## Known gaps and catch-up

Catch-up is the Slack reconcile (today's `services/slack-ingestion/pull_test.py`, becoming `Enricher.reconcile`). It runs per channel from a cursor (`cursors`, stream `channel:<id>`). It reads `conversations.history` since the cursor, then `conversations.replies` for each thread that changed.

| Gap | What happens | Catch-up |
|---|---|---|
| **Slack retries** (intake didn't answer within 3 s) | The same `event_id` arrives again: a 200, nothing stored twice | Nothing needed |
| **Every retry fails** (intake or Postgres down for more than about 5 minutes) | The webhook copy is lost | Reconcile picks the message up from history. Slack's optional "Delayed Events" setting would keep retrying hourly for 24 hours; see [open questions](#open-questions) |
| **Delivery cap** (over 30,000 events per workspace per hour) | Slack drops events and sends `app_rate_limited` once per minute | Enrich enqueues a reconcile for that workspace from `minute_rate_limited` |
| **Edits** | `message_changed` carries the new version, so each edit is kept | Reconcile only sees the latest version (history never returns edit records). Edits in between are lost if their webhooks were missed |
| **Deletes** | `message_deleted` carries `deleted_ts`, and a tombstone is written | Reconcile can't see a delete: the message just isn't in history any more. A missed delete stays missed. This is the "deleted before reconcile ran" row in `architecture.md`'s [failures table](architecture.md#failures-and-replay) |
| **Thread replies** | Each reply is its own `message` event with `thread_ts`. `message_replied` arrives without its `subtype`, so replies are detected by `thread_ts != ts`. A reply can arrive before its parent is stored | Enrich fetches the missing parent. `conversations.history` returns parents but not replies, so reconcile finds threads whose `latest_reply` is past the cursor and reads them with `conversations.replies` |
| **Out of order** (an edit before the original, a reply before its parent, an edit after a delete) | Each one is still written as its own versioned row | The [ordering value](#ordering-value-ember-63) decides which is current |
| **Bot not in the channel** | No events (the bot only reads channels it's invited to) | When the bot is invited (`member_joined_channel` with our bot as `user`), reconcile that channel from the start or its cursor. When it's removed, stop. Data is kept (EMBER-53) |
| **Workspace not mapped to a tenant** | Stored with `tenant = null`, not processed | `ember-admin map` assigns the tenant and enqueues what was stored |
| **Hosted, non-Marketplace app** | Live events are unaffected | Catch-up is slow: `history`/`replies` allow 1 call per minute, 15 messages per page. See [`slack.md`](slack.md#non-marketplace-apps-the-2025-limit-confirmed) |

## Open questions

1. **EMBER-63 (Jacob):** the column's name and type. This doc suggests `timestamptz`, filled as described in [Ordering value](#ordering-value-ember-63).
2. **Turn on "Delayed Events"?** Slack would keep retrying failed deliveries hourly for 24 hours. That covers longer outages without a reconcile, but late events depend even more on the ordering value.
3. **Missed deletes:** is it worth a periodic check that compares recently stored messages with history, to catch deletes whose webhook was lost?
4. **`thread_broadcast`:** treat it as a channel-level statement or only as a thread member (carried over from `slack.md`)?
5. **`message_replied`:** use it to refresh the parent's reply count, or ignore it because each reply arrives on its own?

## Sources

Checked on 2026-10-13:
- [Events API](https://docs.slack.dev/apis/events-api/): the wrapper fields (`event_id` "globally unique across all workspaces"), the 3-second rule, retries (about immediately, then 1 and 5 minutes; `X-Slack-Retry-Num`, `X-Slack-Retry-Reason`; Delayed Events), the 30,000/hour cap and `app_rate_limited`
- [`message`](https://docs.slack.dev/reference/events/message) and its subtypes:
  - [`message_changed`](https://docs.slack.dev/reference/events/message/message_changed)
  - [`message_deleted`](https://docs.slack.dev/reference/events/message/message_deleted)
  - [`message_replied`](https://docs.slack.dev/reference/events/message/message_replied) (the missing-`subtype` bug)
  - [`thread_broadcast`](https://docs.slack.dev/reference/events/message/thread_broadcast)
- Event pages:
  - [`reaction_added`](https://docs.slack.dev/reference/events/reaction_added)
  - [`member_joined_channel`](https://docs.slack.dev/reference/events/member_joined_channel)
  - [`member_left_channel`](https://docs.slack.dev/reference/events/member_left_channel)
  - [`channel_created`](https://docs.slack.dev/reference/events/channel_created)
  - [`channel_rename`](https://docs.slack.dev/reference/events/channel_rename)
  - [`channel_archive`](https://docs.slack.dev/reference/events/channel_archive)
  - [`channel_deleted`](https://docs.slack.dev/reference/events/channel_deleted)
  - [`group_rename`](https://docs.slack.dev/reference/events/group_rename)
  - [`user_change`](https://docs.slack.dev/reference/events/user_change)
  - [`team_join`](https://docs.slack.dev/reference/events/team_join)
  - [`app_uninstalled`](https://docs.slack.dev/reference/events/app_uninstalled)
  - [`tokens_revoked`](https://docs.slack.dev/reference/events/tokens_revoked)
- Methods:
  - [`users.info`](https://docs.slack.dev/reference/methods/users.info) (Tier 4)
  - [`conversations.info`](https://docs.slack.dev/reference/methods/conversations.info) (Tier 3)
  - [`conversations.replies`](https://docs.slack.dev/reference/methods/conversations.replies) (Tier 3; the non-Marketplace limit)
- Retries reuse the `event_id`: Slack's reference doesn't say so directly. Secondary sources do ([Hookdeck](https://hookdeck.com/webhooks/skills/slack-webhooks), [dev.to](https://dev.to/sapotacorp/the-slack-events-api-subscriptions-and-event-handlers-2i61)), and the current receiver's de-duplication depends on it.
- A real delivery: `packages/ingestion-envelope/fixtures/slack.json` (`event_id`, `event_time`, `event.ts`, `event.event_ts`)

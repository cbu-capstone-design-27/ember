# Ingestion payload contract (v1)

Jira: EMBER-39. Schema: `packages/ingestion-envelope/schema/envelope.v1.schema.json`.

Every source emits **one envelope**. There is no separate top-level schema per source. Source-specific fields live in `payload`. Embeddings are not in this envelope (EMBER-3).

Content is referenced, not embedded. `content_refs` holds URLs (`kind: url`) or object pointers (`kind: object`). Do not put issue bodies, message text, or file blobs in the envelope.

## Envelope

| Field | Contents |
| --- | --- |
| `identity.id` | Stable Ember id |
| `identity.source_native_id` | Source-native id, kept correlatable |
| `source.system` | `github`, `jira`, `slack`, `teams`, or `gitlab` |
| `source.org` | Org, site, workspace, or tenant |
| `source.actor` | Source-native actor id |
| `time.occurred_at` | When it happened (RFC 3339) |
| `time.ingested_at` | When Ember stored it (RFC 3339) |
| `content_refs` | URL or object pointer list. May be empty |
| `type` | Event kind (`issue.opened`, `message.posted`, …) |
| `metadata` | Open bag for small non-content facts |
| `payload` | Typed body for `source.system` |
| `raw_ref` | Optional. Omit is valid |

raw_ref (optional): pointer to opaque connector JSON not yet in typed payload. Pointer only — do not inline raw blobs. Embeddings stay out (EMBER-3).

## Payload by source

Native ids also appear inside `payload` so a connector can correlate without parsing `identity` alone.

| System | `payload` fields | `source_native_id` |
| --- | --- | --- |
| GitHub | `repository`, `number`, `title` | `owner/repo#number` |
| Jira | `key`, `project_key`, `issue_type`, `summary` | issue key (`EMBER-39`) |
| Slack | `channel`, `ts` | `channel:ts` |
| Teams | `team_id`, `channel_id`, `message_id` | message id |
| GitLab | `project_path`, `iid`, `title` | `group/project!iid` |

GitHub v1 payload is issue/PR-shaped only (repository / number / title). Push and commits events are out of this contract; follow up under EMBER-35.

## Fixtures

One sample each, under `packages/ingestion-envelope/fixtures/`:

- `github.json`
- `jira.json`
- `slack.json`
- `teams.json`
- `gitlab.json`

```sh
python3 packages/ingestion-envelope/test_contract.py
```

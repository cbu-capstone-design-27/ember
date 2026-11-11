// What the dashboard reads (EMBER-125): one tenant's rows in ingestion's
// `source_records` (docs/ingestion/architecture.md, Storage). This is the only
// module that touches the table, and it only reads. The API from EMBER-115 can
// wrap `loadDashboard` as it is.
//
// Every query filters on `tenant`, the active organization's, and nothing here
// takes a tenant from the request. Postgres only: SQLite has no ingestion tables.
//
// A row is one version of one object, unique on (source, tenant, object_id,
// version), so a pull request edited five times is five rows. Everything below
// reads the newest version of each object, ordered by the source's own time
// (`occurred_at`, then `id`) as the ordering rule requires (EMBER-63). An
// object whose newest version is a tombstone is gone: GitHub and GitLab mark it
// `record.tombstone`, Jira and Slack `record.deleted`.

import type pg from "pg";

/** All the module needs from a pool, so a test can watch the statements it sends. */
export type Queryable = Pick<pg.Pool, "query">;

/** How far back "messages" counts. */
export const MESSAGE_WINDOW_DAYS = 7;
export const ACTIVITY_LIMIT = 14;

export interface Breakdown {
  total: number;
  /** Per source, only those with at least one. */
  bySource: Record<string, number>;
}

export interface Summary {
  openPullRequests: Breakdown;
  openIssues: Breakdown;
  /** Messages from the last MESSAGE_WINDOW_DAYS days. */
  messages: Breakdown;
}

export interface SourceCount {
  source: string;
  /** Objects currently stored, of every kind (a deleted object is not counted). */
  records: number;
  lastActivity: Date;
}

export interface ActivityItem {
  /** `source_records.id` of the version shown. */
  id: number;
  source: string;
  kind: string;
  /** "Pull request", "Comment", … */
  label: string;
  title: string;
  /** Where it lives: "acme/web#42", "EMBER-7", "#general". */
  target: string | null;
  /** Its state, where it has one: "Merged", "Approved". */
  status: string | null;
  actor: string | null;
  excerpt: string | null;
  at: Date;
  /** An http(s) link to the original, when the record has enough to build one. */
  url: string | null;
}

export interface Dashboard {
  summary: Summary;
  sources: SourceCount[];
  activity: ActivityItem[];
}

// The newest version of each of the tenant's objects. Ids only, so the sort
// doesn't carry every payload; `current` joins the rows back (still by tenant).
const CURRENT = `
  latest as (
    select distinct on (source, kind, object_id) id
    from public.source_records
    where tenant = $1
    order by source, kind, object_id, occurred_at desc, id desc
  ),
  current as (
    select r.id, r.source, r.kind, r.occurred_at, r.record,
           coalesce(r.record ->> 'tombstone' = 'true', false) or coalesce(r.record ->> 'deleted' = 'true', false) as gone
    from latest join public.source_records r on r.id = latest.id
    where r.tenant = $1
  )`;

// "Open" by each source's own words: GitHub says open, GitLab opened, and Jira
// has no open state, only a status category (anything not "done").
const IS_OPEN_PULL_REQUEST = `(
  (source = 'github' and kind = 'pull_request' and record #>> '{payload,pull_request,state}' = 'open')
  or (source = 'gitlab' and kind = 'merge_request'
      and coalesce(record #>> '{payload,object_attributes,state}', record #>> '{payload,state}') = 'opened')
)`;

// A pull request GitHub sends as an issue is counted once, as a pull request
const IS_OPEN_ISSUE = `(
  (source = 'github' and kind = 'issue' and record #>> '{payload,issue,state}' = 'open'
   and record #> '{payload,issue,pull_request}' is null)
  or (source = 'gitlab' and kind = 'issue'
      and coalesce(record #>> '{payload,object_attributes,state}', record #>> '{payload,state}') = 'opened')
  or (source = 'jira' and kind = 'issue'
      and coalesce(record #>> '{payload,issue,fields,status,statusCategory,key}',
                   case when lower(record #>> '{payload,issue,fields,status,name}') = 'done' then 'done' else 'new' end) <> 'done')
)`;

/** The kinds the feed shows. The rest (users, labels, refs, commits, …) are context, not activity. */
const ACTIVITY_KINDS = [
  "pull_request",
  "pull_request_review",
  "pull_request_review_comment",
  "issue",
  "issue_comment",
  "merge_request",
  "note",
  "comment",
  "message",
];

interface CountRow {
  source: string;
  records: string;
  last_activity: Date;
  open_pull_requests: string;
  open_issues: string;
  messages: string;
}

interface ActivityRow {
  id: string;
  source: string;
  kind: string;
  object_id: string;
  occurred_at: Date;
  record: unknown;
}

/**
 * The summary numbers and the sources card for a tenant, and its newest
 * activity. `now` is a parameter so tests can fix the clock.
 */
export async function loadDashboard(
  pool: Queryable,
  tenant: string,
  { limit = ACTIVITY_LIMIT, now = new Date() }: { limit?: number; now?: Date } = {},
): Promise<Dashboard> {
  const [counts, recent] = await Promise.all([
    pool.query<CountRow>(
      `with ${CURRENT}
       select source,
              count(*) filter (where not gone) as records,
              max(occurred_at) as last_activity,
              count(*) filter (where not gone and ${IS_OPEN_PULL_REQUEST}) as open_pull_requests,
              count(*) filter (where not gone and ${IS_OPEN_ISSUE}) as open_issues,
              count(*) filter (where not gone and source = 'slack' and kind = 'message'
                               and occurred_at > $2::timestamptz - make_interval(days => $3)) as messages
       from current
       group by source
       order by source`,
      [tenant, now.toISOString(), MESSAGE_WINDOW_DAYS],
    ),
    pool.query<ActivityRow>(
      `with ${CURRENT}
       select s.id, s.source, s.kind, s.object_id, s.occurred_at, s.record
       from (select id from current
             where not gone and kind = any($2::text[])
             order by occurred_at desc, id desc
             limit $3) top
       join public.source_records s on s.id = top.id
       where s.tenant = $1
       order by s.occurred_at desc, s.id desc`,
      [tenant, ACTIVITY_KINDS, limit],
    ),
  ]);

  const breakdown = (column: "open_pull_requests" | "open_issues" | "messages"): Breakdown => {
    const bySource: Record<string, number> = {};
    for (const row of counts.rows) if (Number(row[column]) > 0) bySource[row.source] = Number(row[column]);
    return { total: Object.values(bySource).reduce((a, b) => a + b, 0), bySource };
  };

  return {
    summary: {
      openPullRequests: breakdown("open_pull_requests"),
      openIssues: breakdown("open_issues"),
      messages: breakdown("messages"),
    },
    // A source whose every object was deleted has nothing to show
    sources: counts.rows
      .filter((row) => Number(row.records) > 0)
      .map((row) => ({ source: row.source, records: Number(row.records), lastActivity: row.last_activity })),
    activity: recent.rows.flatMap((row) => {
      const item = describeRecord(row.source, row.kind, row.object_id, row.record);
      return item ? [{ id: Number(row.id), source: row.source, kind: row.kind, at: row.occurred_at, ...item }] : [];
    }),
  };
}

// ---- reading a record -----------------------------------------------------
// `record` is what the source sent, kept as it arrived (`payload`) plus what
// enrich fetched. Each source names things its own way, so each gets a reader.
// Records come from outside, so every read tolerates a missing field.

type Json = Record<string, unknown>;
export type Described = Pick<ActivityItem, "label" | "title" | "target" | "status" | "actor" | "excerpt" | "url">;

function obj(value: unknown): Json {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Json) : {};
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/** First line, cut to `max` characters. */
function brief(value: unknown, max = 140): string | null {
  const line = text(value)?.split(/\r?\n/, 1)[0].trim();
  if (!line) return null;
  return line.length > max ? `${line.slice(0, max - 1).trimEnd()}…` : line;
}

/** Only http(s) links are shown as links: a record can hold any string. */
export function safeUrl(value: unknown): string | null {
  const raw = text(value);
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function origin(value: unknown): string | null {
  const url = safeUrl(value);
  return url ? new URL(url).origin : null;
}

const capital = (word: string) => word.charAt(0).toUpperCase() + word.slice(1).replaceAll("_", " ");

export function describeRecord(source: string, kind: string, objectId: string, record: unknown): Described | null {
  const rec = obj(record);
  if (source === "github") return describeGitHub(kind, objectId, obj(rec.payload));
  if (source === "gitlab") return describeGitLab(kind, obj(rec.payload));
  if (source === "jira") return describeJira(kind, rec);
  if (source === "slack") return describeSlack(kind, rec);
  return null;
}

function describeGitHub(kind: string, objectId: string, payload: Json): Described | null {
  const repo = text(obj(payload.repository).full_name);
  const sender = text(obj(payload.sender).login);
  const login = (user: unknown) => text(obj(user).login) ?? sender;
  const slot = (key: string) => obj(payload[key]);
  if (kind === "pull_request") {
    const pr = slot("pull_request");
    const state = pr.merged === true ? "Merged" : pr.state === "closed" ? "Closed" : pr.draft === true ? "Draft" : "Open";
    return {
      label: "Pull request",
      title: text(pr.title) ?? objectId,
      target: objectId,
      status: state,
      actor: login(pr.user),
      excerpt: null,
      url: safeUrl(pr.html_url),
    };
  }
  if (kind === "issue") {
    const issue = slot("issue");
    return {
      label: "Issue",
      title: text(issue.title) ?? objectId,
      target: objectId,
      status: issue.state === "closed" ? "Closed" : "Open",
      actor: login(issue.user),
      excerpt: null,
      url: safeUrl(issue.html_url),
    };
  }
  if (kind === "issue_comment" || kind === "pull_request_review_comment") {
    const comment = slot("comment");
    const parent = kind === "issue_comment" ? slot("issue") : slot("pull_request");
    return {
      label: kind === "issue_comment" ? "Comment" : "Review comment",
      title: text(parent.title) ?? "Comment",
      target: repo && typeof parent.number === "number" ? `${repo}#${parent.number}` : null,
      status: null,
      actor: login(comment.user),
      excerpt: brief(comment.body, 200),
      url: safeUrl(comment.html_url),
    };
  }
  if (kind === "pull_request_review") {
    const review = slot("review");
    const pr = slot("pull_request");
    const state = text(review.state);
    return {
      label: "Review",
      title: text(pr.title) ?? "Review",
      target: repo && typeof pr.number === "number" ? `${repo}#${pr.number}` : null,
      status: state ? capital(state.toLowerCase()) : null,
      actor: login(review.user),
      excerpt: brief(review.body, 200),
      url: safeUrl(review.html_url),
    };
  }
  return null;
}

function describeGitLab(kind: string, payload: Json): Described | null {
  // A webhook has the object under object_attributes; a REST object (reconcile, backfill) is the body
  const attrs = payload.object_attributes ? obj(payload.object_attributes) : payload;
  const project = text(obj(payload.project).path_with_namespace);
  const actor = text(obj(payload.user).username) ?? text(obj(attrs.author).username);
  const url = safeUrl(attrs.url) ?? safeUrl(attrs.web_url);
  if (kind === "merge_request" || kind === "issue") {
    const mark = kind === "merge_request" ? "!" : "#";
    const state = attrs.state === "merged" ? "Merged" : attrs.state === "closed" ? "Closed" : kind === "merge_request" && (attrs.draft === true || attrs.work_in_progress === true) ? "Draft" : "Open";
    return {
      label: kind === "merge_request" ? "Merge request" : "Issue",
      title: text(attrs.title) ?? `${mark}${attrs.iid ?? ""}`,
      target: project && attrs.iid !== undefined ? `${project}${mark}${attrs.iid}` : null,
      status: state,
      actor,
      excerpt: null,
      url,
    };
  }
  if (kind === "note") {
    const parent = obj(payload.merge_request ?? payload.issue ?? payload.commit);
    return {
      label: "Comment",
      title: text(parent.title) ?? brief(parent.message) ?? "Comment",
      target: null,
      status: null,
      actor,
      excerpt: brief(attrs.note, 200),
      url,
    };
  }
  return null;
}

function describeJira(kind: string, record: Json): Described | null {
  const payload = obj(record.payload);
  if (kind === "issue") {
    const issue = obj(payload.issue);
    const fields = obj(issue.fields);
    const key = text(issue.key);
    const site = origin(issue.self);
    return {
      label: "Issue",
      title: text(fields.summary) ?? key ?? "Issue",
      target: key,
      status: text(obj(fields.status).name),
      actor: text(obj(payload.user).displayName) ?? text(obj(fields.reporter).displayName),
      excerpt: null,
      url: site && key ? `${site}/browse/${encodeURIComponent(key)}` : null,
    };
  }
  if (kind === "comment") {
    const comment = obj(payload.comment);
    // The webhook may carry the issue; otherwise enrich fetched it under its id
    const issue = obj(payload.issue ?? Object.values(obj(record.issues))[0]);
    const key = text(issue.key);
    const site = origin(comment.self);
    return {
      label: "Comment",
      title: text(obj(issue.fields).summary) ?? key ?? "Comment",
      target: key,
      status: null,
      actor: text(obj(comment.author).displayName),
      // Jira's v3 API sends comment bodies as a document, not text; only plain text is shown
      excerpt: brief(comment.body, 200),
      url: site && key ? `${site}/browse/${encodeURIComponent(key)}?focusedCommentId=${encodeURIComponent(String(comment.id ?? ""))}` : null,
    };
  }
  return null;
}

/** Slack's `<@U123>`, `<#C123|general>` and `<https://…|label>` as the words a person sees. */
export function plainSlack(value: string): string {
  return value
    .replace(/<@([A-Z0-9]+)(?:\|([^>]+))?>/g, (_match, _id, name?: string) => `@${name ?? "someone"}`)
    .replace(/<#[A-Z0-9]+\|([^>]+)>/g, "#$1")
    .replace(/<(?:https?:\/\/|mailto:)[^|>]+\|([^>]+)>/g, "$1")
    .replace(/<((?:https?:\/\/|mailto:)[^>]+)>/g, "$1")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

function describeSlack(kind: string, record: Json): Described | null {
  if (kind !== "message") return null;
  const payload = obj(record.payload);
  const message = obj(record.message ?? payload.event);
  const channel = obj(record.channel);
  const user = obj(record.user);
  const profile = obj(user.profile);
  const body = text(message.text);
  const channelId = text(message.channel) ?? text(channel.id) ?? text(obj(payload.event).channel);
  const team = text(payload.team_id);
  const name = text(channel.name);
  return {
    label: "Message",
    title: (body && brief(plainSlack(body))) ?? "(no text)",
    target: name ? `#${name}` : null,
    status: null,
    actor: text(profile.display_name) ?? text(user.real_name) ?? text(user.name),
    excerpt: null,
    // Slack's permalink needs the workspace's domain, which the record doesn't hold: link to the channel
    url: team && channelId ? safeUrl(`https://app.slack.com/client/${encodeURIComponent(team)}/${encodeURIComponent(channelId)}`) : null,
  };
}

// An index over a workspace snapshot: who is who across sources, which pull
// requests belong to which tickets, what each message mentions and says.
// The dashboard (insights.ts, summary.ts) and the graph (graph.ts) both read
// this, so a link found here shows up in both places the same way.

import type { SourceId } from "../sources.ts";
import { extractRefs, mergeRefs, signalsOf, decisionStatement, type Signal, type TextRefs } from "./text.ts";
import type { Issue, Person, PullRequest, SlackMessage, Workspace } from "./types.ts";

/** Graph node ids. Insights point at these so "View in graph" can focus a node. */
export const nodeId = {
  person: (id: string) => `person:${id}`,
  identity: (source: SourceId, handle: string) => `identity:${source}:${handle}`,
  repository: (name: string) => `container:github:${name}`,
  project: (key: string) => `container:jira:${key}`,
  channel: (id: string) => `container:slack:${id}`,
  issue: (key: string) => `workitem:jira:${key}`,
  change: (id: string) => `change:github:${id}`,
  thread: (ts: string) => `conversation:slack:${ts}`,
  message: (ts: string) => `message:slack:${ts}`,
  comment: (id: string) => `message:jira:${id}`,
  decision: (ts: string) => `decision:slack:${ts}`,
  module: (repo: string, path: string) => `module:github:${repo}/${path}`,
};

export type Stage = "todo" | "in_progress" | "in_review" | "done";

/** Where a Jira status sits in the flow, whatever the team calls it. */
export function stageOf(status: string): Stage {
  const s = status.toLowerCase();
  if (/\b(done|closed|resolved|released|complete)\b/.test(s)) return "done";
  if (/\b(review|qa|testing|verify)\b/.test(s)) return "in_review";
  if (/\b(to do|todo|backlog|open|new|selected)\b/.test(s)) return "todo";
  return "in_progress";
}

export function changeId(pr: Pick<PullRequest, "repo" | "number">): string {
  return `${pr.repo}#${pr.number}`;
}

export interface Decision {
  /** The message the decision was made in. */
  ts: string;
  statement: string;
  author?: Person;
  at: string;
  /** Tickets the decision is about (from the message and its thread). */
  issues: string[];
  /** Ticket comments that link back to the decision. */
  recordedIn: Array<{ key: string; commentId: string }>;
}

export interface Model {
  ws: Workspace;
  sources: SourceId[];
  people: Map<string, Person>;
  byAccount: { github: Map<string, Person>; jira: Map<string, Person>; slack: Map<string, Person> };
  issues: Map<string, Issue>;
  changes: Map<string, PullRequest>;
  messages: Map<string, SlackMessage>;
  /** Ticket keys each pull request names in its title or branch. */
  changeIssues: Map<string, string[]>;
  issueChanges: Map<string, string[]>;
  /** What each message mentions itself. */
  messageRefs: Map<string, TextRefs>;
  /** Replies to each thread root, oldest first. */
  replies: Map<string, SlackMessage[]>;
  signals: Map<string, Signal[]>;
  commentRefs: Map<string, TextRefs>;
  decisions: Decision[];
}

/** A message's mentions plus its thread root's: a reply is about what its thread is about. */
export function contextRefs(model: Model, message: SlackMessage): TextRefs {
  const own = model.messageRefs.get(message.ts)!;
  const root = message.threadTs ? model.messageRefs.get(message.threadTs) : undefined;
  return root ? mergeRefs(own, root) : own;
}

/** Keep only the sources the user picked. People stay; their accounts on dropped sources go. */
export function forSources(ws: Workspace, sources: readonly SourceId[]): Workspace {
  const keep = new Set(sources);
  return {
    ...ws,
    people: ws.people.map((p) => ({
      ...p,
      accounts: Object.fromEntries(
        Object.entries(p.accounts).filter(([source]) => keep.has(source as SourceId)),
      ) as Person["accounts"],
    })),
    github: keep.has("github") ? ws.github : undefined,
    jira: keep.has("jira") ? ws.jira : undefined,
    slack: keep.has("slack") ? ws.slack : undefined,
  };
}

export function buildModel(ws: Workspace): Model {
  const sources = (["github", "jira", "slack"] as const).filter((s) => ws[s]);
  const people = new Map(ws.people.map((p) => [p.id, p]));
  const byAccount = { github: new Map<string, Person>(), jira: new Map<string, Person>(), slack: new Map<string, Person>() };
  for (const p of ws.people) {
    for (const source of ["github", "jira", "slack"] as const) {
      const handle = p.accounts[source];
      if (handle) byAccount[source].set(handle, p);
    }
  }

  const issues = new Map((ws.jira?.issues ?? []).map((i) => [i.key, i]));
  const changes = new Map((ws.github?.pullRequests ?? []).map((pr) => [changeId(pr), pr]));
  const messages = new Map((ws.slack?.messages ?? []).map((m) => [m.ts, m]));
  const refContext = {
    // Keys are recognized even when Jira isn't connected, so a PR titled
    // "CHK-142: …" still reads as being about CHK-142.
    projectKeys: [...new Set([ws.jira?.project.key, ...projectKeysIn(ws)].filter((k): k is string => !!k))],
    changes: [...changes.keys()],
  };

  const changeIssues = new Map<string, string[]>();
  const issueChanges = new Map<string, string[]>();
  for (const [id, pr] of changes) {
    const keys = extractRefs(`${pr.title} ${pr.branch}`, refContext).issues;
    changeIssues.set(id, keys);
    for (const key of keys) issueChanges.set(key, [...(issueChanges.get(key) ?? []), id]);
  }

  const messageRefs = new Map<string, TextRefs>();
  const signals = new Map<string, Signal[]>();
  const replies = new Map<string, SlackMessage[]>();
  for (const m of messages.values()) {
    messageRefs.set(m.ts, extractRefs(m.text, refContext));
    signals.set(m.ts, signalsOf(m.text));
    if (m.threadTs) replies.set(m.threadTs, [...(replies.get(m.threadTs) ?? []), m]);
  }
  for (const list of replies.values()) list.sort((a, b) => a.at.localeCompare(b.at));

  const commentRefs = new Map<string, TextRefs>();
  for (const issue of issues.values()) {
    for (const c of issue.comments ?? []) commentRefs.set(c.id, extractRefs(c.body, refContext));
  }

  const model: Model = {
    ws, sources, people, byAccount, issues, changes, messages,
    changeIssues, issueChanges, messageRefs, replies, signals, commentRefs, decisions: [],
  };

  for (const m of messages.values()) {
    if (!signals.get(m.ts)!.includes("decision")) continue;
    const about = contextRefs(model, m).issues;
    const recordedIn: Decision["recordedIn"] = [];
    for (const issue of issues.values()) {
      for (const c of issue.comments ?? []) {
        if (commentRefs.get(c.id)!.messages.includes(m.ts)) recordedIn.push({ key: issue.key, commentId: c.id });
      }
    }
    model.decisions.push({
      ts: m.ts,
      statement: decisionStatement(m.text),
      author: byAccount.slack.get(m.user),
      at: m.at,
      issues: about,
      recordedIn,
    });
  }

  return model;
}

/** Project keys that appear in PR titles, for when Jira itself isn't connected. */
function projectKeysIn(ws: Workspace): string[] {
  const keys = new Set<string>();
  for (const pr of ws.github?.pullRequests ?? []) {
    for (const m of `${pr.title} ${pr.branch}`.matchAll(/\b([A-Z][A-Z0-9]{1,9})-\d+\b/g)) keys.add(m[1]);
  }
  return [...keys];
}

export function personName(model: Model, source: "github" | "jira" | "slack", handle: string | undefined): string {
  if (!handle) return "Unassigned";
  return model.byAccount[source].get(handle)?.name ?? handle;
}

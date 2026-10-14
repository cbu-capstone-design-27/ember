// The dashboard's numbers, work table and activity feed, from the model.

import type { SourceId } from "../sources.ts";
import type { Insight } from "./insights.ts";
import { changeId, contextRefs, nodeId, personName, stageOf, type Model, type Stage } from "./model.ts";
import { plainSlackText } from "./text.ts";
import type { PullRequest } from "./types.ts";

export interface Stats {
  attention: { total: number; high: number };
  pullRequests: { open: number; awaitingReview: number; drafts: number } | null;
  inFlight: { total: number; inReview: number } | null;
  conversations: { linked: number; total: number } | null;
}

export function stats(model: Model, insights: Insight[]): Stats {
  const prs = [...model.changes.values()];
  const issues = [...model.issues.values()].filter((i) => i.type !== "Epic");
  const messages = [...model.messages.values()];
  return {
    attention: { total: insights.length, high: insights.filter((i) => i.severity === "high").length },
    pullRequests: model.ws.github
      ? {
          open: prs.filter((p) => p.state === "open" || p.state === "draft").length,
          // Open, not approved, and not sent back to the author.
          awaitingReview: prs.filter(
            (p) => p.state === "open" && !p.reviews.some((r) => r.state === "approved" || r.state === "changes_requested"),
          ).length,
          drafts: prs.filter((p) => p.state === "draft").length,
        }
      : null,
    inFlight: model.ws.jira
      ? {
          total: issues.filter((i) => ["in_progress", "in_review"].includes(stageOf(i.status))).length,
          inReview: issues.filter((i) => stageOf(i.status) === "in_review").length,
        }
      : null,
    conversations: model.ws.slack
      ? {
          linked: messages.filter((m) => {
            const r = contextRefs(model, m);
            return r.issues.length > 0 || r.changes.length > 0;
          }).length,
          total: messages.length,
        }
      : null,
  };
}

export interface WorkRow {
  key: string;
  summary: string;
  type: string;
  status: string;
  stage: Stage;
  priority: string;
  assignee: string;
  change?: {
    id: string;
    state: PullRequest["state"];
    checks: PullRequest["checks"];
    review: "approved" | "changes_requested" | "waiting" | "none";
  };
  lastMention?: { at: string; channel: string };
  insights: number;
  nodeId: string;
}

/** Tickets being worked on, most recently active first. */
export function workInFlight(model: Model, insights: Insight[]): WorkRow[] {
  const flagged = new Map<string, number>();
  for (const i of insights) flagged.set(i.subject, (flagged.get(i.subject) ?? 0) + 1);
  const channelName = new Map((model.ws.slack?.channels ?? []).map((c) => [c.id, c.name]));

  return [...model.issues.values()]
    .filter((i) => i.type !== "Epic" && ["in_progress", "in_review"].includes(stageOf(i.status)))
    .map((issue) => {
      const prs = (model.issueChanges.get(issue.key) ?? []).map((id) => model.changes.get(id)!);
      const pr = prs.find((p) => p.state === "open") ?? prs.find((p) => p.state === "draft") ?? prs.find((p) => p.state === "merged");
      const mentions = [...model.messages.values()]
        .filter((m) => contextRefs(model, m).issues.includes(issue.key))
        .sort((a, b) => b.at.localeCompare(a.at));
      const activity = [issue.updatedAt, pr?.updatedAt, mentions[0]?.at].filter((x): x is string => !!x).sort().pop()!;
      const row: WorkRow & { activity: string } = {
        key: issue.key,
        summary: issue.summary,
        type: issue.type,
        status: issue.status,
        stage: stageOf(issue.status),
        priority: issue.priority,
        assignee: personName(model, "jira", issue.assignee),
        change: pr && {
          id: changeId(pr),
          state: pr.state,
          checks: pr.checks,
          review: pr.reviews.some((r) => r.state === "changes_requested")
            ? "changes_requested"
            : pr.reviews.some((r) => r.state === "approved")
              ? "approved"
              : pr.reviews.length
                ? "waiting"
                : "none",
        },
        lastMention: mentions[0] && { at: mentions[0].at, channel: channelName.get(mentions[0].channel) ?? "" },
        insights: flagged.get(nodeId.issue(issue.key)) ?? 0,
        nodeId: nodeId.issue(issue.key),
        activity,
      };
      return row;
    })
    .sort((a, b) => b.activity.localeCompare(a.activity))
    .map(({ activity: _, ...row }) => row);
}

export interface ActivityItem {
  id: string;
  source: SourceId;
  at: string;
  actor: string;
  /** "merged", "moved CHK-127 to In Review", "posted in #checkout-dev". */
  action: string;
  target: string;
  /** Message text or item title. */
  text?: string;
  nodeId: string;
}

/** Everything that happened across sources, newest first. */
export function activity(model: Model, limit = 14): ActivityItem[] {
  const items: ActivityItem[] = [];
  for (const pr of model.changes.values()) {
    const id = changeId(pr);
    const target = nodeId.change(id);
    const author = personName(model, "github", pr.author);
    items.push({ id: `pr-open:${id}`, source: "github", at: pr.createdAt, actor: author, action: pr.state === "draft" ? "opened a draft" : "opened", target: id, text: pr.title, nodeId: target });
    if (pr.mergedAt) items.push({ id: `pr-merge:${id}`, source: "github", at: pr.mergedAt, actor: author, action: "merged", target: id, text: pr.title, nodeId: target });
    for (const r of pr.reviews) {
      if (!r.at || r.state === "requested") continue;
      const verb = { approved: "approved", changes_requested: "requested changes on", commented: "reviewed" }[r.state];
      items.push({ id: `review:${id}:${r.reviewer}`, source: "github", at: r.at, actor: personName(model, "github", r.reviewer), action: verb, target: id, text: pr.title, nodeId: target });
    }
  }
  for (const issue of model.issues.values()) {
    for (const h of issue.history) {
      items.push({ id: `status:${issue.key}:${h.at}`, source: "jira", at: h.at, actor: personName(model, "jira", h.by), action: `moved to ${h.to}`, target: issue.key, text: issue.summary, nodeId: nodeId.issue(issue.key) });
    }
    for (const c of issue.comments ?? []) {
      items.push({ id: `comment:${c.id}`, source: "jira", at: c.at, actor: personName(model, "jira", c.author), action: "commented on", target: issue.key, text: c.body, nodeId: nodeId.comment(c.id) });
    }
  }
  const channelName = new Map((model.ws.slack?.channels ?? []).map((c) => [c.id, c.name]));
  const nameOf = (uid: string) => model.byAccount.slack.get(uid)?.name;
  for (const m of model.messages.values()) {
    items.push({
      id: `msg:${m.ts}`,
      source: "slack",
      at: m.at,
      actor: personName(model, "slack", m.user),
      action: m.threadTs ? "replied in" : "posted in",
      target: `#${channelName.get(m.channel) ?? m.channel}`,
      text: plainSlackText(m.text, nameOf),
      nodeId: nodeId.message(m.ts),
    });
  }
  return items.sort((a, b) => b.at.localeCompare(a.at)).slice(0, limit);
}

export interface SourceSummary {
  id: SourceId;
  /** "3 repositories · 11 pull requests"; null when there is no data for it yet. */
  detail: string | null;
}

export function sourceSummaries(model: Model, sources: readonly SourceId[]): SourceSummary[] {
  const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  return sources.map((id) => {
    const { github, jira, slack } = model.ws;
    if (id === "github" && github) return { id, detail: `${plural(github.repositories.length, "repository", "repositories")} · ${plural(github.pullRequests.length, "pull request")}` };
    if (id === "jira" && jira) return { id, detail: `${jira.project.key} · ${plural(jira.issues.length, "issue")}` };
    if (id === "slack" && slack) return { id, detail: `${plural(slack.channels.length, "channel")} · ${plural(slack.messages.length, "message")}` };
    return { id, detail: null };
  });
}

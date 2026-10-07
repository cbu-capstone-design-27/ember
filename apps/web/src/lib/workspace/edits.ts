// Changes a user makes to the preview workspace: move a ticket, flag it,
// comment, re-run checks, link a PR, post in Slack. Each is a small record
// replayed on top of the snapshot, so the dashboard and the graph recompute
// from the result exactly as they would from real data. Today the records
// live in the browser (preview-store.tsx); with real sources each one
// becomes a call to that source's API.

import type { Person, Workspace } from "./types.ts";

export type EditBody =
  | { kind: "issue_status"; key: string; to: string }
  | { kind: "issue_flag"; key: string; flagged: boolean }
  | { kind: "issue_comment"; key: string; body: string }
  | { kind: "pr_title"; change: string; title: string }
  | { kind: "pr_checks"; change: string; checks: "passing" | "failing" | "pending" }
  | { kind: "slack_message"; channel: string; text: string; threadTs?: string };

export type Edit = EditBody & { id: string; at: string };

/** The signed-in user, who makes the edits. */
export interface Me {
  id: string;
  name: string;
  email: string;
}

/** The user's account on each source, as the edits record it. */
export function meAccounts(me: Me) {
  const handle = me.email.split("@")[0].toLowerCase().replace(/[^a-z0-9-]/g, "-") || "you";
  return { github: handle, jira: `acc-${me.id}`, slack: `U${me.id.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 10)}` };
}

/** Slack ts for a message the user posts: the edit's time, plus digits from its id, so it stays put if other edits are undone. */
export function editTs(edit: Pick<Edit, "id" | "at">): string {
  let h = 0;
  for (const ch of edit.id) h = (h * 31 + ch.charCodeAt(0)) % 100000;
  return `${Math.floor(Date.parse(edit.at) / 1000)}.9${String(h).padStart(5, "0")}`;
}

/** The workspace with the edits applied, oldest first. Edits whose target isn't there (a source was dropped) are skipped. */
export function applyEdits(ws: Workspace, edits: readonly Edit[], me: Me): Workspace {
  if (edits.length === 0) return ws;
  const out: Workspace = structuredClone(ws);
  const handles = meAccounts(me);
  const accounts: Person["accounts"] = {};
  if (out.github) accounts.github = handles.github;
  if (out.jira) accounts.jira = handles.jira;
  if (out.slack) accounts.slack = handles.slack;
  out.people.push({ id: `me-${me.id}`, name: me.name, title: "You", email: me.email, accounts });

  const issues = new Map((out.jira?.issues ?? []).map((i) => [i.key, i]));
  const prs = new Map((out.github?.pullRequests ?? []).map((p) => [`${p.repo}#${p.number}`, p]));

  [...edits]
    .sort((a, b) => a.at.localeCompare(b.at))
    .forEach((edit) => {
      switch (edit.kind) {
        case "issue_status": {
          const issue = issues.get(edit.key);
          if (!issue || issue.status === edit.to) return;
          issue.history.push({ at: edit.at, by: handles.jira, from: issue.status, to: edit.to });
          issue.status = edit.to;
          issue.updatedAt = edit.at;
          return;
        }
        case "issue_flag": {
          const issue = issues.get(edit.key);
          if (!issue) return;
          issue.flagged = edit.flagged;
          issue.updatedAt = edit.at;
          return;
        }
        case "issue_comment": {
          const issue = issues.get(edit.key);
          if (!issue) return;
          (issue.comments ??= []).push({ id: edit.id, author: handles.jira, at: edit.at, body: edit.body });
          issue.updatedAt = edit.at;
          return;
        }
        case "pr_title": {
          const pr = prs.get(edit.change);
          if (!pr) return;
          pr.title = edit.title;
          pr.updatedAt = edit.at;
          return;
        }
        case "pr_checks": {
          const pr = prs.get(edit.change);
          if (!pr) return;
          pr.checks = edit.checks;
          if (edit.checks !== "failing") delete pr.failingChecks;
          pr.updatedAt = edit.at;
          return;
        }
        case "slack_message": {
          if (!out.slack?.channels.some((c) => c.id === edit.channel)) return;
          out.slack.messages.push({
            ts: editTs(edit),
            channel: edit.channel,
            user: handles.slack,
            at: edit.at,
            text: edit.text,
            ...(edit.threadTs ? { threadTs: edit.threadTs } : {}),
          });
          return;
        }
      }
    });
  return out;
}

/** Slack's permalink for a message, as a ticket comment would quote it. */
export function slackPermalink(ws: Workspace, channel: string, ts: string): string {
  return `https://${ws.slack?.workspace ?? "slack"}.slack.com/archives/${channel}/p${ts.replace(".", "")}`;
}

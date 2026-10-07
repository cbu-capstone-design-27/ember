// Insights: places where the sources disagree or work is stuck, with the
// evidence from each source. Each rule only needs the sources it reads, so a
// team without Jira still gets the GitHub and Slack ones.

import type { SourceId } from "../sources.ts";
import { contextRefs, nodeId, personName, stageOf, type Model } from "./model.ts";
import { plainSlackText, titleSimilarity } from "./text.ts";
import { ago, DAY, duration } from "./time.ts";
import type { Issue, PullRequest, SlackMessage } from "./types.ts";

export type InsightKind = "blocker" | "failing_checks" | "status_drift" | "stale_review" | "missing_link" | "decision";
export type Severity = "high" | "medium" | "low";

export const INSIGHT_KINDS: Record<InsightKind, { label: string; order: number }> = {
  blocker: { label: "Blockers", order: 0 },
  failing_checks: { label: "Failing checks", order: 1 },
  status_drift: { label: "Status drift", order: 2 },
  stale_review: { label: "Waiting on review", order: 3 },
  missing_link: { label: "Missing links", order: 4 },
  decision: { label: "Decisions", order: 5 },
};

export interface Evidence {
  source: SourceId;
  nodeId: string;
  /** "checkout-web#219", "CHK-131", "#checkout-dev". */
  label: string;
  /** "Merged 5h ago", "In Development", "Priya Natarajan · 4h ago". */
  detail: string;
  /** Title of the item, or the message text. */
  text: string;
  quote: boolean;
  at?: string;
}

export interface Insight {
  id: string;
  kind: InsightKind;
  severity: Severity;
  title: string;
  summary: string;
  /** The graph node the insight is about. */
  subject: string;
  /** When the newest piece of evidence happened. */
  at: string;
  evidence: Evidence[];
  /** What a person would do about it. Actions run once sources are connected. */
  action: string;
}

/** Open pull requests nobody has reviewed for this long are flagged. */
export const STALE_REVIEW_MS = 3 * DAY;

const SEVERITY_RANK: Record<Severity, number> = { high: 0, medium: 1, low: 2 };

export function findInsights(model: Model, now: number): Insight[] {
  const found = [
    ...blockers(model, now),
    ...failingChecks(model, now),
    ...statusDrift(model, now),
    ...staleReviews(model, now),
    ...missingLinks(model, now),
    ...decisions(model, now),
  ];
  return found.sort(
    (a, b) =>
      SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] ||
      INSIGHT_KINDS[a.kind].order - INSIGHT_KINDS[b.kind].order ||
      b.at.localeCompare(a.at),
  );
}

// --- evidence --------------------------------------------------------------

function issueEvidence(model: Model, issue: Issue): Evidence {
  return {
    source: "jira",
    nodeId: nodeId.issue(issue.key),
    label: issue.key,
    detail: issue.status,
    text: issue.summary,
    quote: false,
    at: issue.updatedAt,
  };
}

function prState(pr: PullRequest, now: number): string {
  if (pr.state === "merged" && pr.mergedAt) return `Merged ${ago(pr.mergedAt, now)}`;
  if (pr.state === "draft") return "Draft";
  if (pr.state === "closed") return "Closed";
  return `Opened ${ago(pr.createdAt, now)}`;
}

function changeEvidence(model: Model, pr: PullRequest, now: number, detail = prState(pr, now)): Evidence {
  return {
    source: "github",
    nodeId: nodeId.change(`${pr.repo}#${pr.number}`),
    label: `${pr.repo}#${pr.number}`,
    detail,
    text: pr.title,
    quote: false,
    at: pr.mergedAt ?? pr.updatedAt,
  };
}

function messageEvidence(model: Model, m: SlackMessage, now: number): Evidence {
  const channel = model.ws.slack?.channels.find((c) => c.id === m.channel)?.name ?? m.channel;
  return {
    source: "slack",
    nodeId: nodeId.message(m.ts),
    label: `#${channel}`,
    detail: `${personName(model, "slack", m.user)} · ${ago(m.at, now)}`,
    text: plainSlackText(m.text, (id) => model.byAccount.slack.get(id)?.name),
    quote: true,
    at: m.at,
  };
}

function channelName(model: Model, m: SlackMessage): string {
  return `#${model.ws.slack?.channels.find((c) => c.id === m.channel)?.name ?? m.channel}`;
}

function newest(evidence: Evidence[]): string {
  return evidence.reduce((max, e) => (e.at && e.at > max ? e.at : max), "");
}

/** Messages whose (thread) context mentions the issue and that carry the signal. */
function messagesSaying(model: Model, key: string, signal: "done" | "blocked"): SlackMessage[] {
  return [...model.messages.values()]
    .filter((m) => model.signals.get(m.ts)!.includes(signal) && contextRefs(model, m).issues.includes(key))
    .sort((a, b) => b.at.localeCompare(a.at));
}

function workIssues(model: Model): Issue[] {
  return [...model.issues.values()].filter((i) => i.type !== "Epic");
}

// --- rules -----------------------------------------------------------------

/** Someone says a ticket is blocked, and Jira doesn't show it. */
function blockers(model: Model, now: number): Insight[] {
  const out: Insight[] = [];
  for (const issue of workIssues(model)) {
    if (issue.flagged || stageOf(issue.status) === "done") continue;
    const [said] = messagesSaying(model, issue.key, "blocked");
    if (!said) continue;
    const who = personName(model, "slack", said.user);
    const blocks = (issue.links ?? []).filter((l) => l.type === "blocks").map((l) => l.key);
    const evidence = [messageEvidence(model, said, now), issueEvidence(model, issue)];
    for (const key of blocks) {
      const other = model.issues.get(key);
      if (other) evidence.push(issueEvidence(model, other));
    }
    out.push({
      id: `blocker:${issue.key}`,
      kind: "blocker",
      severity: "high",
      title: `${issue.key} is blocked, but Jira doesn't show it`,
      summary:
        `${who} said so in ${channelName(model, said)} ${ago(said.at, now)}. Jira still shows ${issue.status}, not flagged.` +
        (blocks.length ? ` It also blocks ${blocks.join(", ")}.` : ""),
      subject: nodeId.issue(issue.key),
      at: newest(evidence),
      evidence,
      action: `Flag ${issue.key} as blocked`,
    });
  }
  return out;
}

/** An open pull request with failing checks; worse when its ticket is already in review. */
function failingChecks(model: Model, now: number): Insight[] {
  const out: Insight[] = [];
  for (const [id, pr] of model.changes) {
    if (pr.state !== "open" || pr.checks !== "failing") continue;
    const issues = (model.changeIssues.get(id) ?? []).map((k) => model.issues.get(k)).filter((i): i is Issue => !!i);
    const inReview = issues.find((i) => ["in_review", "done"].includes(stageOf(i.status)));
    const checks = pr.failingChecks?.length ? pr.failingChecks.join(", ") : "A required check";
    const evidence = [changeEvidence(model, pr, now, `Checks failing: ${checks}`), ...issues.map((i) => issueEvidence(model, i))];
    for (const issue of issues) {
      for (const c of issue.comments ?? []) {
        if (Date.parse(c.at) >= Date.parse(pr.createdAt)) {
          evidence.push({
            source: "jira",
            nodeId: nodeId.comment(c.id),
            label: `${issue.key} comment`,
            detail: `${personName(model, "jira", c.author)} · ${ago(c.at, now)}`,
            text: c.body,
            quote: true,
            at: c.at,
          });
        }
      }
    }
    out.push({
      id: `failing_checks:${id}`,
      kind: "failing_checks",
      severity: inReview ? "high" : "medium",
      title: inReview
        ? `Checks are failing on ${id}, and ${inReview.key} is ${inReview.status}`
        : `Checks are failing on ${id}`,
      summary: `${checks} fails on the latest commit. ${
        inReview ? `The ticket says the work is ready, but it can't merge as it is.` : "It can't merge until that passes."
      }`,
      subject: inReview ? nodeId.issue(inReview.key) : nodeId.change(id),
      at: newest(evidence),
      evidence,
      action: inReview ? `Fix ${checks}, or move ${inReview.key} back to In Development` : `Fix ${checks}`,
    });
  }
  return out;
}

/** Jira is behind what GitHub and Slack show. */
function statusDrift(model: Model, now: number): Insight[] {
  const out: Insight[] = [];
  for (const issue of workIssues(model)) {
    const stage = stageOf(issue.status);
    if (stage === "done") continue;
    const prs = (model.issueChanges.get(issue.key) ?? []).map((id) => model.changes.get(id)!);
    const merged = prs.filter((p) => p.state === "merged").sort((a, b) => b.mergedAt!.localeCompare(a.mergedAt!));
    const open = prs.filter((p) => p.state === "open");
    const [saidDone] = messagesSaying(model, issue.key, "done");
    const base = { subject: nodeId.issue(issue.key), kind: "status_drift" as const };

    if (merged.length && open.length === 0) {
      const pr = merged[0];
      const evidence = [
        ...(saidDone ? [messageEvidence(model, saidDone, now)] : []),
        changeEvidence(model, pr, now),
        issueEvidence(model, issue),
      ];
      const who = saidDone ? personName(model, "slack", saidDone.user) : undefined;
      out.push({
        ...base,
        id: `status_drift:${issue.key}`,
        severity: "medium",
        title: saidDone
          ? `${issue.key} looks done, but Jira says ${issue.status}`
          : `${pr.repo}#${pr.number} merged, but ${issue.key} is still ${issue.status}`,
        summary: saidDone
          ? `${who} said it's done in ${channelName(model, saidDone)} ${ago(saidDone.at, now)}, and ${pr.repo}#${pr.number} merged ${ago(pr.mergedAt!, now)}.`
          : `The pull request merged ${ago(pr.mergedAt!, now)} and nothing else is open for the ticket.`,
        at: newest(evidence),
        evidence,
        action: `Move ${issue.key} to Done`,
      });
    } else if (saidDone && !merged.length && !open.length) {
      const evidence = [messageEvidence(model, saidDone, now), issueEvidence(model, issue)];
      out.push({
        ...base,
        id: `status_drift:${issue.key}`,
        severity: "medium",
        title: `Slack says ${issue.key} is done, Jira says ${issue.status}`,
        summary: `${personName(model, "slack", saidDone.user)} said so in ${channelName(model, saidDone)} ${ago(saidDone.at, now)}. No pull request is linked to the ticket.`,
        at: newest(evidence),
        evidence,
        action: `Check ${issue.key} and move it to Done`,
      });
    } else if (open.length && (stage === "todo" || stage === "in_progress")) {
      const pr = open.sort((a, b) => a.createdAt.localeCompare(b.createdAt))[0];
      const evidence = [changeEvidence(model, pr, now), issueEvidence(model, issue)];
      const asked = [...model.messages.values()]
        .filter((m) => model.messageRefs.get(m.ts)!.changes.includes(`${pr.repo}#${pr.number}`))
        .sort((a, b) => b.at.localeCompare(a.at))[0];
      if (asked) evidence.unshift(messageEvidence(model, asked, now));
      out.push({
        ...base,
        id: `status_drift:${issue.key}`,
        severity: "medium",
        title: `A pull request is open, but ${issue.key} is still ${issue.status}`,
        summary: `${pr.repo}#${pr.number} has been ready for review for ${duration(now - Date.parse(pr.createdAt))}. Jira still shows the work as ${issue.status.toLowerCase()}.`,
        at: newest(evidence),
        evidence,
        action: `Move ${issue.key} to In Review`,
      });
    }
  }
  return out;
}

/** Ready-for-review pull requests nobody has reviewed in STALE_REVIEW_MS. */
function staleReviews(model: Model, now: number): Insight[] {
  const out: Insight[] = [];
  for (const [id, pr] of model.changes) {
    if (pr.state !== "open") continue;
    const waited = now - Date.parse(pr.createdAt);
    if (waited < STALE_REVIEW_MS || pr.reviews.some((r) => r.state !== "requested")) continue;
    const requested = pr.reviews.filter((r) => r.state === "requested").map((r) => personName(model, "github", r.reviewer));
    const issues = (model.changeIssues.get(id) ?? []).map((k) => model.issues.get(k)).filter((i): i is Issue => !!i);
    const evidence = [changeEvidence(model, pr, now), ...issues.map((i) => issueEvidence(model, i))];
    const nudges = [...model.messages.values()]
      .filter((m) => model.messageRefs.get(m.ts)!.changes.includes(id))
      .sort((a, b) => b.at.localeCompare(a.at));
    if (nudges[0]) evidence.unshift(messageEvidence(model, nudges[0], now));
    out.push({
      id: `stale_review:${id}`,
      kind: "stale_review",
      severity: waited > 7 * DAY ? "high" : "medium",
      title: `${id} has waited ${duration(waited)} for a review`,
      summary:
        (requested.length ? `Review requested from ${listOf(requested)}, and nobody has reviewed yet.` : "Nobody is requested to review it.") +
        (nudges.length ? ` It came up in Slack ${nudges.length === 1 ? "once" : nudges.length === 2 ? "twice" : `${nudges.length} times`}.` : ""),
      subject: nodeId.change(id),
      at: newest(evidence),
      evidence,
      action: requested.length ? `Nudge ${requested[0]}` : "Request a reviewer",
    });
  }
  return out;
}

/** A ticket in review that no pull request names, with the likeliest match. */
function missingLinks(model: Model, now: number): Insight[] {
  if (!model.ws.github) return [];
  const out: Insight[] = [];
  const unlinked = [...model.changes].filter(([id, pr]) => pr.state !== "closed" && !(model.changeIssues.get(id) ?? []).length);
  for (const issue of workIssues(model)) {
    if (stageOf(issue.status) !== "in_review" || (model.issueChanges.get(issue.key) ?? []).length) continue;
    const best = unlinked
      .map(([id, pr]) => ({ id, pr, score: titleSimilarity(issue.summary, pr.title) }))
      .sort((a, b) => b.score - a.score)[0];
    const match = best && best.score >= 0.5 ? best : undefined;
    const evidence = [issueEvidence(model, issue)];
    if (match) evidence.push(changeEvidence(model, match.pr, now));
    out.push({
      id: `missing_link:${issue.key}`,
      kind: "missing_link",
      severity: "low",
      title: `${issue.key} is In Review, but no pull request mentions it`,
      summary: match
        ? `${match.id} "${match.pr.title}" looks like the one. It doesn't name the ticket, so nothing links them.`
        : "Without a link, reviewers can't get from the ticket to the code.",
      subject: nodeId.issue(issue.key),
      at: newest(evidence),
      evidence,
      action: match ? `Add ${issue.key} to the title of ${match.id}` : `Link a pull request to ${issue.key}`,
    });
  }
  return out;
}

/** A decision made in Slack that no ticket records. */
function decisions(model: Model, now: number): Insight[] {
  const out: Insight[] = [];
  for (const d of model.decisions) {
    if (d.recordedIn.length || !d.issues.length) continue;
    const m = model.messages.get(d.ts)!;
    const issues = d.issues.map((k) => model.issues.get(k)).filter((i): i is Issue => !!i);
    const evidence = [messageEvidence(model, m, now), ...issues.map((i) => issueEvidence(model, i))];
    const key = d.issues[0];
    out.push({
      id: `decision:${d.ts}`,
      kind: "decision",
      severity: "low",
      title: `A decision about ${key} isn't on the ticket`,
      summary: `${d.author?.name ?? "Someone"} decided in ${channelName(model, m)} ${ago(d.at, now)}: "${d.statement}"`,
      subject: nodeId.decision(d.ts),
      at: newest(evidence),
      evidence,
      action: `Add the decision to ${key}`,
    });
  }
  return out;
}

function listOf(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

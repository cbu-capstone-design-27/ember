// Real records from the preview workspace for the landing page and the
// log-in panel: the CHK-131 drift, a search over the data in the shape of
// the MCP server's `search` hits (apps/mcp, EMBER-79), and a small slice of
// the knowledge graph. Server-side only: it reads the preview snapshot.

import type { SourceId } from "../../lib/sources.ts";
import { buildGraph, type EdgeType, type NodeType } from "../../lib/workspace/graph.ts";
import { findInsights } from "../../lib/workspace/insights.ts";
import { loadPreviewWorkspace } from "../../lib/workspace/load.ts";
import { buildModel, changeId, nodeId, personName } from "../../lib/workspace/model.ts";
import { plainSlackText } from "../../lib/workspace/text.ts";
import { activity, sourceSummaries, type ActivityItem } from "../../lib/workspace/summary.ts";
import { ago } from "../../lib/workspace/time.ts";

const SOURCES: SourceId[] = ["github", "jira", "slack"];

export const DRIFT_KEY = "CHK-131";

/** One record in the drift story, as its source shows it. */
export interface DriftCard {
  source: "slack" | "github" | "jira";
  /** Where it lives: a channel, a PR, a ticket key. Mono. */
  where: string;
  /** The author, for a message. */
  who?: string;
  text: string;
  /** The state that matters: "Merged", "In Development". */
  state?: string;
  when: string;
}

export interface Drift {
  cards: { slack: DriftCard; github: DriftCard; jira: DriftCard };
  /** The insight Ember raises, word for word. */
  flag: string;
  action: string;
}

/** The MCP `search` hit fields the landing shows. */
export interface SearchHit {
  source: SourceId;
  kind: "issue" | "pull_request" | "message";
  object_id: string;
  /** How to show where it lives: the object id, or #channel for a message. */
  label: string;
  title: string;
  /** With the matched words in **bold**, like ts_headline. */
  snippet: string;
}

export interface MiniGraph {
  nodes: Array<{ id: string; type: NodeType; label: string; focus?: boolean }>;
  edges: Array<{ id: string; source: string; target: string; type: EdgeType }>;
}

function load() {
  const { workspace, now } = loadPreviewWorkspace(SOURCES);
  const model = buildModel(workspace);
  return { model, now };
}

export function driftStory(): Drift {
  const { model, now } = load();
  const issue = model.issues.get(DRIFT_KEY)!;
  const prId = (model.issueChanges.get(DRIFT_KEY) ?? [])[0];
  const pr = model.changes.get(prId)!;
  const message = [...model.messages.values()]
    .filter((m) => /done/i.test(m.text) && m.text.includes(DRIFT_KEY))
    .sort((a, b) => b.at.localeCompare(a.at))[0];
  const channel = model.ws.slack!.channels.find((c) => c.id === message.channel)!;
  const since = issue.history.at(-1)?.at ?? issue.updatedAt;
  const insight = findInsights(model, now).find((i) => i.kind === "status_drift" && i.subject === nodeId.issue(DRIFT_KEY))!;
  const firstSentence = (text: string) => text.split(/(?<=[.!?])\s/)[0];
  return {
    cards: {
      slack: {
        source: "slack",
        where: `#${channel.name}`,
        who: personName(model, "slack", message.user),
        text: firstSentence(plainSlackText(message.text, (uid) => model.byAccount.slack.get(uid)?.name)),
        when: ago(message.at, now),
      },
      github: {
        source: "github",
        where: changeId(pr),
        text: pr.title,
        state: "Merged",
        when: ago(pr.mergedAt!, now),
      },
      jira: {
        source: "jira",
        where: issue.key,
        text: issue.summary,
        state: issue.status,
        when: `since ${ago(since, now)}`,
      },
    },
    flag: insight.title,
    action: insight.actions[0]?.label ?? `Move ${DRIFT_KEY} to Done`,
  };
}

/** A plain word search over the preview records, shaped like the MCP server's hits. */
export function searchPreview(question: string, k = 3, order: SearchHit["kind"][] = ["issue", "pull_request", "message"]): SearchHit[] {
  const { model } = load();
  const words = question.toLowerCase().split(/\s+/).filter(Boolean);
  const bold = (text: string) => words.reduce((t, w) => t.replace(new RegExp(`(${w}\\w*)`, "ig"), "**$1**"), text);
  const has = (text: string) => words.every((w) => text.toLowerCase().includes(w));
  const hits: SearchHit[] = [];
  for (const i of model.issues.values()) {
    const text = `${i.key} ${i.summary} ${i.description}`;
    if (has(text)) hits.push({ source: "jira", kind: "issue", object_id: i.key, label: i.key, title: `${i.key} ${i.summary}`, snippet: bold(i.description) });
  }
  for (const p of model.changes.values()) {
    if (has(p.title)) hits.push({ source: "github", kind: "pull_request", object_id: `${model.ws.github!.org}/${changeId(p)}`, label: changeId(p), title: p.title, snippet: bold(p.title) });
  }
  for (const m of model.messages.values()) {
    const text = plainSlackText(m.text, (uid) => model.byAccount.slack.get(uid)?.name);
    const channel = model.ws.slack!.channels.find((c) => c.id === m.channel)?.name ?? m.channel;
    if (has(text)) hits.push({ source: "slack", kind: "message", object_id: `${m.channel}/${m.ts}`, label: `#${channel}`, title: text.slice(0, 80), snippet: bold(text) });
  }
  return hits.sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind)).slice(0, k);
}

/** The drift ticket and everything one step from it, for the landing's live graph. */
export function miniGraph(): MiniGraph {
  const { model, now } = load();
  const graph = buildGraph(model, now);
  const focus = nodeId.issue(DRIFT_KEY);
  const near = new Set([focus]);
  for (const e of graph.edges) {
    if (e.source === focus) near.add(e.target);
    if (e.target === focus) near.add(e.source);
  }
  // One more step out from the PR and the people, so the picture has shape.
  for (const e of graph.edges) {
    if (near.size > 26) break;
    const from = near.has(e.source) ? e.target : near.has(e.target) ? e.source : null;
    const n = from && graph.nodes.find((x) => x.id === from);
    if (n && (n.type === "Person" || n.type === "Change" || n.type === "Message")) near.add(from!);
  }
  const nodes = graph.nodes
    .filter((n) => near.has(n.id) && n.type !== "Identity")
    .map((n) => ({ id: n.id, type: n.type, label: n.label, focus: n.id === focus || undefined }));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({ id: e.id, source: e.source, target: e.target, type: e.type }));
  return { nodes, edges };
}

export interface ConnectStory {
  tenant: string;
  nodes: number;
  edges: number;
  /** Every launch source, in catalog order. `detail` is null where there's no data yet. */
  sources: Array<{ id: SourceId; detail: string | null; status: "linked" | "no preview data" | "coming" }>;
}

/** The sources linking into one graph, for the sign-up panel. Teams has no ingestion yet (docs only), so it says so. */
export function connectStory(): ConnectStory {
  const { model, now } = load();
  const graph = buildGraph(model, now);
  const all: SourceId[] = ["github", "gitlab", "jira", "slack", "teams"];
  const details = new Map(sourceSummaries(model, all).map((s) => [s.id, s.detail ?? null]));
  return {
    tenant: model.ws.organization.tenant,
    nodes: graph.nodes.length,
    edges: graph.edges.length,
    sources: all.map((id) => ({
      id,
      detail: details.get(id) ?? null,
      status: id === "teams" ? "coming" : details.get(id) ? "linked" : "no preview data",
    })),
  };
}

/** A teammate's "why" question, as an agent answers it through Ember's MCP `search`. */
export interface AgentAsk {
  agent: "cursor" | "claude" | "codex";
  who: string;
  question: string;
  /** What the agent passed to `search`. */
  query: string;
  /** How many records `search` returned (k = 8). */
  count: number;
  /** The best hit, as the agent would cite it. */
  top: SearchHit;
}

export function agentAsks(): AgentAsk[] {
  const asks: Array<Omit<AgentAsk, "count" | "top">> = [
    { agent: "cursor", who: "Lena", question: "Why do we cap webhook retries at 8?", query: "webhook retries" },
    { agent: "claude", who: "Priya", question: "What's blocking CHK-145?", query: "CHK-145 blocked" },
    { agent: "codex", who: "Theo", question: "Why is checkout a single page now?", query: "single-page" },
  ];
  return asks.map((a) => {
    // "Why" questions are answered by what people said, so messages rank first.
    const hits = searchPreview(a.query, 8, ["message", "issue", "pull_request"]);
    return { ...a, count: hits.length, top: hits[0] };
  });
}

/** The landing's feature cards: small, real artifacts from the preview workspace. */
export interface FeatureData {
  timeline: Array<{ source: SourceId; when: string; text: string; hot?: boolean }>;
  decision: { who: string; where: string; text: string; issues: string[] };
  person: { name: string; title: string; accounts: Array<{ source: SourceId; handle: string }> };
  activity: Array<Pick<ActivityItem, "id" | "source" | "actor" | "action" | "target"> & { when: string }>;
  counts: { nodes: number; edges: number; subgraphs: string[] };
}

export function featureData(): FeatureData {
  const { model, now } = load();
  const drift = driftStory();
  const issue = model.issues.get(DRIFT_KEY)!;
  const moved = issue.history.at(-1)!;
  const timeline: FeatureData["timeline"] = [
    { source: "jira", when: ago(moved.at, now), text: `${issue.key} moved to ${moved.to}` },
    { source: "github", when: drift.cards.github.when, text: `${drift.cards.github.where} merged` },
    { source: "slack", when: drift.cards.slack.when, text: `${drift.cards.slack.where}: “${drift.cards.slack.text}”` },
    { source: "jira", when: "now", text: `${issue.key} still ${issue.status}`, hot: true },
  ];

  const d = model.decisions.find((x) => x.statement.toLowerCase().includes("webhook")) ?? model.decisions[0];
  const m = model.messages.get(d.ts)!;
  const channel = model.ws.slack!.channels.find((c) => c.id === m.channel)!;

  const p = model.people.get("priya") ?? [...model.people.values()][0];
  const accounts = (["github", "jira", "slack"] as const).filter((src) => p.accounts[src]).map((src) => ({ source: src as SourceId, handle: p.accounts[src]! }));

  const graph = buildGraph(model, now);
  return {
    timeline,
    decision: { who: personName(model, "slack", m.user), where: `#${channel.name}`, text: d.statement, issues: d.issues },
    person: { name: p.name, title: p.title, accounts },
    activity: activity(model, 7).map((a) => ({ id: a.id, source: a.source, actor: a.actor, action: a.action, target: a.target, when: ago(a.at, now) })),
    counts: { nodes: graph.nodes.length, edges: graph.edges.length, subgraphs: [...new Set(graph.nodes.map((n) => n.subgraph).filter((x): x is string => !!x))] },
  };
}

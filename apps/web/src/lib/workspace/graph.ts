// The workspace as a knowledge graph, in the ontology's terms
// (docs/graph-schema.md, services/pipeline/ontology/): nine node types,
// relationship names from edges.py, and only the endpoint pairs it allows.
// When the graph API exists, the graph view reads the same shape from Neo4j.

import type { SourceId } from "../sources.ts";
import { changeId, nodeId, personName, stageOf, type Model } from "./model.ts";
import { plainSlackText } from "./text.ts";
import { ago, stamp } from "./time.ts";

export type NodeType =
  | "Person" | "Identity" | "Container" | "WorkItem" | "Change"
  | "Conversation" | "Message" | "Decision" | "Module";

export type EdgeType =
  | "RESOLVES_TO" | "AUTHORED" | "ASSIGNED_TO" | "REVIEWED" | "MEMBER_OF" | "OWNS"
  | "CONTAINS" | "PART_OF" | "REPLIES_TO" | "REFERENCES" | "RESOLVES" | "RELATES_TO"
  | "TOUCHES" | "DECIDED_IN" | "SUPERSEDES" | "AFFECTS";

export const NODE_TYPES: Record<NodeType, { label: string; plural: string; description: string }> = {
  WorkItem: { label: "Work item", plural: "Work items", description: "Issues, stories and epics" },
  Change: { label: "Change", plural: "Changes", description: "Pull requests and merge requests" },
  Message: { label: "Message", plural: "Messages", description: "Chat messages and comments" },
  Person: { label: "Person", plural: "People", description: "A human, across all their accounts" },
  Decision: { label: "Decision", plural: "Decisions", description: "Choices the team made" },
  Conversation: { label: "Conversation", plural: "Conversations", description: "Threads" },
  Container: { label: "Container", plural: "Containers", description: "Repositories, projects, channels" },
  Module: { label: "Module", plural: "Modules", description: "Parts of the codebase" },
  Identity: { label: "Identity", plural: "Identities", description: "One account on one source" },
};

export interface GraphField {
  label: string;
  value: string;
}

export interface GraphNode {
  id: string;
  type: NodeType;
  /** Missing for Person, Decision and Module, which are resolved or extracted, not copied from one source. */
  source?: SourceId;
  /** Short label drawn on the canvas. */
  label: string;
  /** Full name or title, for the detail panel. */
  title: string;
  /** "Story · In Review", "Pull request · Open". */
  subtitle: string;
  /** Longer text: a description or the message itself. */
  body?: string;
  fields: GraphField[];
  /** Graphiti group_id the node lives in: <tenant>_<source>. */
  subgraph?: string;
  at?: string;
  /** For styling: open / merged / draft, or a ticket's stage. */
  state?: string;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: EdgeType;
  /** Edge attribute, e.g. a review verdict or a link type. */
  detail?: string;
}

export interface KnowledgeGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export function buildGraph(model: Model, now: number): KnowledgeGraph {
  const { ws } = model;
  const nodes = new Map<string, GraphNode>();
  const edges = new Map<string, GraphEdge>();
  const tenant = ws.organization.tenant;
  const subgraph = (source: SourceId) => `${tenant}_${source}`;

  const addNode = (node: GraphNode) => {
    if (!nodes.has(node.id)) nodes.set(node.id, node);
  };
  const addEdge = (source: string, type: EdgeType, target: string, detail?: string) => {
    if (source === target || !nodes.has(source) || !nodes.has(target)) return;
    const id = `${source}|${type}|${target}`;
    if (!edges.has(id)) edges.set(id, { id, source, target, type, detail });
  };
  const when = (iso: string) => `${ago(iso, now)} (${stamp(iso)})`;

  // People, and the accounts that resolve to them.
  for (const p of ws.people) {
    const accounts = Object.entries(p.accounts) as Array<[SourceId, string]>;
    if (accounts.length === 0) continue;
    addNode({
      id: nodeId.person(p.id),
      type: "Person",
      label: p.name,
      title: p.name,
      subtitle: p.title,
      fields: [
        { label: "Email", value: p.email },
        { label: "Accounts", value: accounts.map(([s, h]) => `${s}: ${h}`).join(", ") },
      ],
    });
    for (const [source, handle] of accounts) {
      const id = nodeId.identity(source, handle);
      addNode({
        id,
        type: "Identity",
        source,
        label: handle,
        title: `${handle} on ${sourceName(source)}`,
        subtitle: `Identity · ${sourceName(source)}`,
        fields: [{ label: "Resolves to", value: p.name }],
        subgraph: subgraph(source),
      });
      addEdge(id, "RESOLVES_TO", nodeId.person(p.id));
    }
  }
  const person = (source: "github" | "jira" | "slack", handle?: string) => {
    const p = handle ? model.byAccount[source].get(handle) : undefined;
    return p ? nodeId.person(p.id) : undefined;
  };

  // Jira: the project, its issues and their comments.
  if (ws.jira) {
    const project = nodeId.project(ws.jira.project.key);
    addNode({
      id: project,
      type: "Container",
      source: "jira",
      label: ws.jira.project.key,
      title: `${ws.jira.project.name} (${ws.jira.project.key})`,
      subtitle: "Jira project",
      fields: [{ label: "Site", value: ws.jira.site }],
      subgraph: subgraph("jira"),
    });
    for (const issue of model.issues.values()) {
      addNode({
        id: nodeId.issue(issue.key),
        type: "WorkItem",
        source: "jira",
        label: issue.key,
        title: `${issue.key}: ${issue.summary}`,
        subtitle: `${issue.type} · ${issue.status}`,
        body: issue.description,
        fields: [
          { label: "Status", value: issue.status },
          { label: "Priority", value: issue.priority },
          { label: "Assignee", value: personName(model, "jira", issue.assignee) },
          { label: "Reporter", value: personName(model, "jira", issue.reporter) },
          ...(issue.epic ? [{ label: "Epic", value: issue.epic }] : []),
          { label: "Updated", value: when(issue.updatedAt) },
        ],
        subgraph: subgraph("jira"),
        at: issue.updatedAt,
        state: issue.type === "Epic" ? "epic" : stageOf(issue.status),
      });
    }
    for (const issue of model.issues.values()) {
      const id = nodeId.issue(issue.key);
      addEdge(project, "CONTAINS", id);
      const assignee = person("jira", issue.assignee);
      if (assignee) addEdge(assignee, "ASSIGNED_TO", id);
      const reporter = person("jira", issue.reporter);
      if (reporter) addEdge(reporter, "AUTHORED", id);
      if (issue.epic) addEdge(id, "RELATES_TO", nodeId.issue(issue.epic), "epic");
      for (const link of issue.links ?? []) addEdge(id, "RELATES_TO", nodeId.issue(link.key), link.type);
      for (const c of issue.comments ?? []) {
        const cid = nodeId.comment(c.id);
        addNode({
          id: cid,
          type: "Message",
          source: "jira",
          label: `${issue.key} comment`,
          title: `Comment on ${issue.key}`,
          subtitle: `Jira comment · ${personName(model, "jira", c.author)}`,
          body: c.body,
          fields: [
            { label: "Author", value: personName(model, "jira", c.author) },
            { label: "Posted", value: when(c.at) },
          ],
          subgraph: subgraph("jira"),
          at: c.at,
        });
        addEdge(cid, "REFERENCES", id, "comment on");
        const author = person("jira", c.author);
        if (author) addEdge(author, "AUTHORED", cid);
      }
    }
  }

  // GitHub: repositories, pull requests, and the code they touch.
  if (ws.github) {
    for (const repo of ws.github.repositories) {
      addNode({
        id: nodeId.repository(repo.name),
        type: "Container",
        source: "github",
        label: repo.name,
        title: `${ws.github.org}/${repo.name}`,
        subtitle: "GitHub repository",
        body: repo.description,
        fields: [],
        subgraph: subgraph("github"),
      });
    }
    for (const pr of model.changes.values()) {
      const id = nodeId.change(changeId(pr));
      addNode({
        id,
        type: "Change",
        source: "github",
        label: `${pr.repo.replace(/^checkout-/, "")}#${pr.number}`,
        title: `${pr.repo}#${pr.number}: ${pr.title}`,
        subtitle: `Pull request · ${pr.state === "merged" ? "Merged" : pr.state === "draft" ? "Draft" : pr.state === "closed" ? "Closed" : "Open"}`,
        fields: [
          { label: "Author", value: personName(model, "github", pr.author) },
          { label: "Branch", value: `${pr.branch} → ${pr.base}` },
          { label: "Checks", value: pr.checks === "failing" ? `Failing (${(pr.failingChecks ?? []).join(", ")})` : capitalize(pr.checks) },
          { label: "Size", value: `+${pr.additions} −${pr.deletions}` },
          { label: pr.mergedAt ? "Merged" : "Opened", value: when(pr.mergedAt ?? pr.createdAt) },
        ],
        subgraph: subgraph("github"),
        at: pr.mergedAt ?? pr.updatedAt,
        state: pr.state,
      });
      addEdge(nodeId.repository(pr.repo), "CONTAINS", id);
      const author = person("github", pr.author);
      if (author) addEdge(author, "AUTHORED", id);
      for (const r of pr.reviews) {
        const reviewer = person("github", r.reviewer);
        if (!reviewer) continue;
        if (r.state === "requested") addEdge(reviewer, "ASSIGNED_TO", id, "review requested");
        else addEdge(reviewer, "REVIEWED", id, r.state.replace("_", " "));
      }
      for (const key of model.changeIssues.get(changeId(pr)) ?? []) addEdge(id, "RESOLVES", nodeId.issue(key));
      for (const path of pr.paths) {
        const mid = nodeId.module(pr.repo, path);
        addNode({
          id: mid,
          type: "Module",
          label: path.split("/").slice(-1)[0],
          title: `${pr.repo}/${path}`,
          subtitle: `Module · ${pr.repo}`,
          fields: [{ label: "Path", value: path }],
          subgraph: subgraph("github"),
        });
        addEdge(nodeId.repository(pr.repo), "CONTAINS", mid);
        addEdge(id, "TOUCHES", mid);
      }
    }
  }

  // Slack: channels, threads, messages, and the decisions made in them.
  if (ws.slack) {
    const channelName = new Map(ws.slack.channels.map((c) => [c.id, c.name]));
    for (const c of ws.slack.channels) {
      addNode({
        id: nodeId.channel(c.id),
        type: "Container",
        source: "slack",
        label: `#${c.name}`,
        title: `#${c.name}`,
        subtitle: "Slack channel",
        body: c.topic,
        fields: [],
        subgraph: subgraph("slack"),
      });
    }
    const nameOf = (uid: string) => model.byAccount.slack.get(uid)?.name;
    for (const m of model.messages.values()) {
      const text = plainSlackText(m.text, nameOf);
      const author = personName(model, "slack", m.user);
      addNode({
        id: nodeId.message(m.ts),
        type: "Message",
        source: "slack",
        label: truncate(text, 28),
        title: `${author} in #${channelName.get(m.channel)}`,
        subtitle: `Slack message${m.threadTs ? " · reply" : ""}`,
        body: text,
        fields: [
          { label: "Author", value: author },
          { label: "Channel", value: `#${channelName.get(m.channel)}` },
          { label: "Posted", value: when(m.at) },
          ...(model.signals.get(m.ts)!.length ? [{ label: "Signals", value: model.signals.get(m.ts)!.join(", ") }] : []),
        ],
        subgraph: subgraph("slack"),
        at: m.at,
      });
    }
    for (const [rootTs, replies] of model.replies) {
      const root = model.messages.get(rootTs);
      if (!root) continue;
      const tid = nodeId.thread(rootTs);
      addNode({
        id: tid,
        type: "Conversation",
        source: "slack",
        label: "Thread",
        title: `Thread in #${channelName.get(root.channel)}`,
        subtitle: `Slack thread · ${replies.length + 1} messages`,
        body: plainSlackText(root.text, nameOf),
        fields: [
          { label: "Started by", value: personName(model, "slack", root.user) },
          { label: "Last reply", value: when(replies[replies.length - 1].at) },
        ],
        subgraph: subgraph("slack"),
        at: replies[replies.length - 1].at,
      });
      addEdge(nodeId.channel(root.channel), "CONTAINS", tid);
      addEdge(nodeId.message(rootTs), "PART_OF", tid);
      for (const r of replies) {
        addEdge(nodeId.message(r.ts), "PART_OF", tid);
        addEdge(nodeId.message(r.ts), "REPLIES_TO", nodeId.message(rootTs));
      }
    }
    for (const m of model.messages.values()) {
      const id = nodeId.message(m.ts);
      const author = person("slack", m.user);
      if (author) addEdge(author, "AUTHORED", id);
      const refs = model.messageRefs.get(m.ts)!;
      for (const key of refs.issues) addEdge(id, "REFERENCES", nodeId.issue(key));
      for (const cid of refs.changes) addEdge(id, "REFERENCES", nodeId.change(cid));
      for (const uid of refs.people) {
        const p = person("slack", uid);
        if (p) addEdge(id, "REFERENCES", p);
      }
    }
    for (const d of model.decisions) {
      const did = nodeId.decision(d.ts);
      const m = model.messages.get(d.ts)!;
      addNode({
        id: did,
        type: "Decision",
        label: truncate(d.statement, 30),
        title: d.statement,
        subtitle: d.recordedIn.length ? "Decision · recorded on a ticket" : "Decision · only in Slack",
        body: plainSlackText(m.text, nameOf),
        fields: [
          { label: "Decided by", value: d.author?.name ?? "Unknown" },
          { label: "Decided", value: when(d.at) },
          { label: "Status", value: "accepted" },
          { label: "Recorded in", value: d.recordedIn.map((r) => r.key).join(", ") || "Nowhere yet" },
        ],
        subgraph: subgraph("slack"),
        at: d.at,
      });
      addEdge(did, "DECIDED_IN", nodeId.message(d.ts));
      if (d.author) addEdge(nodeId.person(d.author.id), "AUTHORED", did);
      for (const key of d.issues) addEdge(did, "AFFECTS", nodeId.issue(key));
      for (const r of d.recordedIn) addEdge(nodeId.comment(r.commentId), "REFERENCES", did);
    }
  }

  // Jira comments that mention pull requests.
  for (const issue of model.issues.values()) {
    for (const c of issue.comments ?? []) {
      const refs = model.commentRefs.get(c.id)!;
      for (const cid of refs.changes) addEdge(nodeId.comment(c.id), "REFERENCES", nodeId.change(cid));
      for (const key of refs.issues) if (key !== issue.key) addEdge(nodeId.comment(c.id), "REFERENCES", nodeId.issue(key));
    }
  }

  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

function sourceName(source: SourceId): string {
  return { github: "GitHub", gitlab: "GitLab", jira: "Jira", slack: "Slack", teams: "Teams" }[source];
}

function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// The shape of a workspace snapshot: what Ember knows about one team's
// GitHub, Jira and Slack. Today it comes from src/data/preview-workspace.json
// (made-up data); later it is read from the knowledge graph. Each section is
// optional because a team only connects the sources it uses.
//
// The records are a simplified form of what the connectors emit. Field names
// follow the sources (GitHub login, Jira account id, Slack user id) so the
// mapping to graph nodes (lib/workspace/graph.ts) is the same one the
// pipeline makes (docs/graph-schema.md).

export interface Workspace {
  /** Time the snapshot was taken. Timestamps are shifted so this reads as "now". */
  anchor: string;
  organization: { name: string; tenant: string };
  people: Person[];
  github?: GitHubData;
  jira?: JiraData;
  slack?: SlackData;
}

export interface Person {
  id: string;
  name: string;
  title: string;
  email: string;
  /** This person's account on each source, by the id that source uses. */
  accounts: { github?: string; jira?: string; slack?: string };
}

export interface GitHubData {
  org: string;
  repositories: Repository[];
  pullRequests: PullRequest[];
}

export interface Repository {
  name: string;
  description: string;
}

export type PullRequestState = "draft" | "open" | "merged" | "closed";
export type ReviewState = "requested" | "approved" | "changes_requested" | "commented";

export interface PullRequest {
  repo: string;
  number: number;
  title: string;
  /** GitHub login. */
  author: string;
  state: PullRequestState;
  branch: string;
  base: string;
  createdAt: string;
  updatedAt: string;
  mergedAt?: string;
  checks: "passing" | "failing" | "pending";
  /** Failing check names, when checks are failing. */
  failingChecks?: string[];
  reviews: Array<{ reviewer: string; state: ReviewState; at?: string }>;
  additions: number;
  deletions: number;
  /** Directories the change touches. */
  paths: string[];
}

export interface JiraData {
  site: string;
  project: { key: string; name: string };
  issues: Issue[];
}

export interface Issue {
  key: string;
  type: "Epic" | "Story" | "Task" | "Bug";
  summary: string;
  description: string;
  status: string;
  priority: "Highest" | "High" | "Medium" | "Low";
  /** Jira account ids. */
  assignee?: string;
  reporter: string;
  epic?: string;
  flagged?: boolean;
  createdAt: string;
  updatedAt: string;
  links?: Array<{ type: string; key: string }>;
  history: Array<{ at: string; by: string; from: string; to: string }>;
  comments?: Array<{ id: string; author: string; at: string; body: string }>;
}

export interface SlackData {
  workspace: string;
  channels: Channel[];
  messages: SlackMessage[];
}

export interface Channel {
  id: string;
  name: string;
  topic: string;
}

export interface SlackMessage {
  /** Slack's ts: unique within the channel, and the message's id. */
  ts: string;
  channel: string;
  /** Slack user id. */
  user: string;
  at: string;
  text: string;
  /** ts of the thread's first message, for replies. */
  threadTs?: string;
  reactions?: Array<{ name: string; count: number }>;
}

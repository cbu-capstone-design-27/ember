// The sources a team can connect to Ember: the launch sources in
// docs/graph-schema.md. A user picks the ones their team uses during
// onboarding; the choice is stored on the user (`user.sources`) and decides
// which data the dashboard and the graph show.
//
// Used on the server (auth validation) and in the browser (the picker), so
// keep this file free of server-only imports.

export const SOURCE_IDS = ["github", "gitlab", "jira", "slack", "teams"] as const;

export type SourceId = (typeof SOURCE_IDS)[number];

export interface SourceInfo {
  id: SourceId;
  name: string;
  category: "Code" | "Work tracking" | "Chat";
  description: string;
  /** What Ember reads from it, in the ontology's terms (graph-schema.md). */
  reads: string[];
  /** The preview workspace has data for it. */
  preview: boolean;
}

export const SOURCES: readonly SourceInfo[] = [
  {
    id: "github",
    name: "GitHub",
    category: "Code",
    description: "Pull requests, reviews, issues and comments from the repositories you install the Ember app on.",
    reads: ["Pull requests and reviews", "Issues and comments", "Which code each change touches"],
    preview: true,
  },
  {
    id: "gitlab",
    name: "GitLab",
    category: "Code",
    description: "Merge requests, reviews, issues and notes from the projects you connect.",
    reads: ["Merge requests and approvals", "Issues and notes", "Which code each change touches"],
    preview: false,
  },
  {
    id: "jira",
    name: "Jira",
    category: "Work tracking",
    description: "Issues, epics, status changes and comments from your Jira projects.",
    reads: ["Issues and epics", "Status history", "Comments and issue links"],
    preview: true,
  },
  {
    id: "slack",
    name: "Slack",
    category: "Chat",
    description: "Messages and threads in the channels you invite Ember to. Nothing else.",
    reads: ["Channel messages and threads", "Decisions and blockers", "Mentions of tickets and PRs"],
    preview: true,
  },
  {
    id: "teams",
    name: "Microsoft Teams",
    category: "Chat",
    description: "Channel messages and replies in the teams Ember is added to.",
    reads: ["Channel messages and replies", "Decisions and blockers", "Mentions of tickets and PRs"],
    preview: false,
  },
];

const BY_ID = new Map(SOURCES.map((s) => [s.id, s]));

export function isSourceId(value: unknown): value is SourceId {
  return typeof value === "string" && BY_ID.has(value as SourceId);
}

export function sourceInfo(id: SourceId): SourceInfo {
  return BY_ID.get(id)!;
}

/**
 * Check a user's source selection: a non-empty list of known ids. Returns the
 * list deduplicated and in catalog order, or the reason it was refused.
 */
export function parseSources(value: unknown): { ok: true; value: SourceId[] } | { ok: false; message: string } {
  if (!Array.isArray(value)) return { ok: false, message: "sources must be a list" };
  const unknown = value.filter((v) => !isSourceId(v));
  if (unknown.length > 0) return { ok: false, message: `Unknown source: ${String(unknown[0])}` };
  if (value.length === 0) return { ok: false, message: "Pick at least one source" };
  return { ok: true, value: SOURCE_IDS.filter((id) => value.includes(id)) };
}

/** The same check as a Standard Schema, which Better Auth runs on sign-up and profile updates. */
export const sourcesSchema = {
  "~standard": {
    version: 1 as const,
    vendor: "ember",
    validate(value: unknown) {
      const result = parseSources(value);
      return result.ok ? { value: result.value } : { issues: [{ message: result.message }] };
    },
  },
};

/** The user's saved selection, tolerating rows written before the field existed. */
export function userSources(user: { sources?: unknown }): SourceId[] {
  const raw = typeof user.sources === "string" ? safeJson(user.sources) : user.sources;
  return Array.isArray(raw) ? SOURCE_IDS.filter((id) => raw.includes(id)) : [];
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

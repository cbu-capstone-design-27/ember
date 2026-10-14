// Reading references and intent out of free text (PR titles, branch names,
// Slack messages, Jira comments). Keys and links are deterministic. The
// intent signals are keyword rules, a stand-in for the extraction step the
// pipeline will run; they only have to be right for clear statements.

export interface TextRefs {
  /** Jira issue keys, e.g. CHK-142. */
  issues: string[];
  /** Pull requests as repo#number. */
  changes: string[];
  /** Slack user ids from <@U…> mentions. */
  people: string[];
  /** Slack message ts values, from permalinks. */
  messages: string[];
}

export interface RefContext {
  /** Jira project keys; other KEY-123 shapes (UTF-8, ISO-8601) are ignored. */
  projectKeys: string[];
  /** Known pull requests as repo#number. */
  changes: string[];
}

const ISSUE_KEY = /\b([A-Z][A-Z0-9]{1,9})-(\d+)\b/g;
const REPO_PR = /\b([a-z0-9][a-z0-9._-]*)#(\d+)\b/gi;
const PR_URL = /github\.com\/[\w.-]+\/([\w.-]+)\/pull\/(\d+)/gi;
const BARE_PR = /(?:^|[^\w#])#(\d+)\b/g;
const MENTION = /<@([A-Z0-9]+)(?:\|[^>]*)?>/g;
const PERMALINK = /https:\/\/[\w-]+\.slack\.com\/archives\/[A-Z0-9]+\/p(\d{10})(\d{6})/g;

export function extractRefs(text: string, context: RefContext): TextRefs {
  const projects = new Set(context.projectKeys);
  const known = new Set(context.changes);
  const issues = new Set<string>();
  const changes = new Set<string>();

  for (const [, project, number] of text.matchAll(ISSUE_KEY)) {
    if (projects.has(project)) issues.add(`${project}-${number}`);
  }
  for (const [, repo, number] of [...text.matchAll(PR_URL), ...text.matchAll(REPO_PR)]) {
    const id = `${repo.toLowerCase()}#${number}`;
    if (known.has(id)) changes.add(id);
  }
  // A bare #123 counts only when exactly one known repository has that number.
  for (const [, number] of text.matchAll(BARE_PR)) {
    const matches = context.changes.filter((id) => id.endsWith(`#${number}`));
    if (matches.length === 1) changes.add(matches[0]);
  }

  return {
    issues: [...issues],
    changes: [...changes],
    people: [...new Set([...text.matchAll(MENTION)].map((m) => m[1]))],
    messages: [...new Set([...text.matchAll(PERMALINK)].map((m) => `${m[1]}.${m[2]}`))],
  };
}

export function mergeRefs(...all: TextRefs[]): TextRefs {
  const merge = (pick: (r: TextRefs) => string[]) => [...new Set(all.flatMap(pick))];
  return {
    issues: merge((r) => r.issues),
    changes: merge((r) => r.changes),
    people: merge((r) => r.people),
    messages: merge((r) => r.messages),
  };
}

export type Signal = "done" | "blocked" | "decision";

const DONE = /\b(done|shipped|merged|landed|is live|released|finished|completed?)\b/i;
const NOT_DONE = /\b(not|isn't|isnt|aren't|wasn't|never|almost|nearly)\s+(yet\s+)?(done|shipped|merged|landed|live|released|finished|complete)/i;
const BLOCKED = /\b(blocked|blocker|stuck|waiting on)\b/i;
const NOT_BLOCKED = /\b(unblocked|no longer blocked|not blocked)\b/i;
const DECISION = /\b(decision|decided|we're going with|we are going with|we go with|agreed to)\b/i;

/** What a message says about the work it mentions. */
export function signalsOf(text: string): Signal[] {
  const signals: Signal[] = [];
  if (DONE.test(text) && !NOT_DONE.test(text)) signals.push("done");
  if (BLOCKED.test(text) && !NOT_BLOCKED.test(text)) signals.push("blocked");
  if (DECISION.test(text)) signals.push("decision");
  return signals;
}

/**
 * The decision itself, without the lead-in: "For CHK-142, decision: we cap
 * retries…" becomes "We cap retries…". First sentence only.
 */
export function decisionStatement(text: string): string {
  const after = text.split(/\bdecision\s*:\s*/i)[1] ?? text;
  const sentence = (after.match(/^.*?[.!?](?=\s|$)/)?.[0] ?? after).trim();
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

/** Slack's markup to plain text, with mentions replaced by names. */
export function plainSlackText(text: string, nameOf: (userId: string) => string | undefined): string {
  return text
    .replace(MENTION, (_, id: string) => `@${nameOf(id) ?? id}`)
    .replace(/<(https?:[^|>]+)\|([^>]+)>/g, "$2")
    .replace(/<(https?:[^>]+)>/g, "$1");
}

const STOP_WORDS = new Set(
  "a an and are as at be by for from in is it of on or the to with update add fix make use".split(" "),
);

function words(text: string): Set<string> {
  return new Set(
    text
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, " ")
      .split(/[\s-]+/)
      .filter((w) => w.length > 2 && !STOP_WORDS.has(w)),
  );
}

/** Word overlap between two titles, 0 to 1 (Jaccard on meaningful words). */
export function titleSimilarity(a: string, b: string): number {
  const wa = words(a);
  const wb = words(b);
  if (wa.size === 0 || wb.size === 0) return 0;
  let shared = 0;
  for (const w of wa) if (wb.has(w)) shared++;
  return shared / (wa.size + wb.size - shared);
}

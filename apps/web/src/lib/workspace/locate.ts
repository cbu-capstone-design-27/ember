// From a graph node id back to the record it was built from, so the detail
// views can show the full item and offer the actions that fit it.

import type { Decision, Model } from "./model.ts";
import type { Channel, Issue, Person, PullRequest, Repository, SlackMessage } from "./types.ts";

export type Located =
  | { type: "issue"; issue: Issue }
  | { type: "pr"; id: string; pr: PullRequest }
  | { type: "message"; message: SlackMessage }
  | { type: "comment"; issue: Issue; comment: NonNullable<Issue["comments"]>[number] }
  | { type: "thread"; root: SlackMessage }
  | { type: "channel"; channel: Channel }
  | { type: "repo"; repo: Repository }
  | { type: "project"; key: string }
  | { type: "person"; person: Person }
  | { type: "decision"; decision: Decision }
  | { type: "module"; repo: string; path: string };

export function locate(model: Model, id: string): Located | null {
  const [kind, source, ...rest] = id.split(":");
  const key = rest.join(":");
  switch (kind) {
    case "workitem": {
      const issue = model.issues.get(key);
      return issue ? { type: "issue", issue } : null;
    }
    case "change": {
      const pr = model.changes.get(key);
      return pr ? { type: "pr", id: key, pr } : null;
    }
    case "message": {
      if (source === "slack") {
        const message = model.messages.get(key);
        return message ? { type: "message", message } : null;
      }
      for (const issue of model.issues.values()) {
        const comment = issue.comments?.find((c) => c.id === key);
        if (comment) return { type: "comment", issue, comment };
      }
      return null;
    }
    case "conversation": {
      const root = model.messages.get(key);
      return root ? { type: "thread", root } : null;
    }
    case "container": {
      if (source === "slack") {
        const channel = model.ws.slack?.channels.find((c) => c.id === key);
        return channel ? { type: "channel", channel } : null;
      }
      if (source === "github") {
        const repo = model.ws.github?.repositories.find((r) => r.name === key);
        return repo ? { type: "repo", repo } : null;
      }
      return model.ws.jira?.project.key === key ? { type: "project", key } : null;
    }
    case "person": {
      const person = model.people.get(source);
      return person ? { type: "person", person } : null;
    }
    case "identity": {
      const person = model.byAccount[source as "github" | "jira" | "slack"]?.get(key);
      return person ? { type: "person", person } : null;
    }
    case "decision": {
      const decision = model.decisions.find((d) => d.ts === key);
      return decision ? { type: "decision", decision } : null;
    }
    case "module": {
      const slash = key.indexOf("/");
      return { type: "module", repo: key.slice(0, slash), path: key.slice(slash + 1) };
    }
    default:
      return null;
  }
}

// The preview workspace and what Ember reads out of it: references, insights,
// dashboard summaries and the knowledge graph. Pure functions, no server.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";
import { SOURCE_IDS, parseSources, userSources } from "../src/lib/sources.ts";
import { applyEdits, type Edit, type EditBody } from "../src/lib/workspace/edits.ts";
import { buildGraph, type EdgeType, type NodeType } from "../src/lib/workspace/graph.ts";
import { findInsights } from "../src/lib/workspace/insights.ts";
import { buildModel, forSources, stageOf } from "../src/lib/workspace/model.ts";
import { activity, stats, workInFlight } from "../src/lib/workspace/summary.ts";
import { decisionStatement, extractRefs, signalsOf, titleSimilarity } from "../src/lib/workspace/text.ts";
import { ago, rebase } from "../src/lib/workspace/time.ts";
import type { Workspace } from "../src/lib/workspace/types.ts";

const snapshot: Workspace = JSON.parse(readFileSync(new URL("../src/data/preview-workspace.json", import.meta.url), "utf8"));
const NOW = Date.parse("2030-01-15T12:00:00Z");
const workspace = rebase(snapshot, snapshot.anchor, new Date(NOW));
const model = buildModel(workspace);
const insights = findInsights(model, NOW);
const byId = new Map(insights.map((i) => [i.id, i]));

describe("reading text", () => {
  const context = { projectKeys: ["CHK"], changes: ["checkout-api#476", "checkout-web#219", "checkout-api#484"] };

  test("finds ticket keys of known projects only", () => {
    assert.deepEqual(extractRefs("CHK-131 is done, UTF-8 and ISO-8601 are not tickets", context).issues, ["CHK-131"]);
  });

  test("finds pull requests as repo#n, as links, and as a bare #n when only one repo has it", () => {
    assert.deepEqual(extractRefs("see checkout-api#476", context).changes, ["checkout-api#476"]);
    assert.deepEqual(
      extractRefs("https://github.com/kestrel-labs/checkout-web/pull/219", context).changes,
      ["checkout-web#219"],
    );
    assert.deepEqual(extractRefs("until #484 ships", context).changes, ["checkout-api#484"]);
    assert.deepEqual(extractRefs("orders 88412 and #9999", context).changes, []);
  });

  test("finds mentions and Slack permalinks", () => {
    const refs = extractRefs(
      "<@U0SAM> see https://kestrel-labs.slack.com/archives/C0DESIGN/p1759161600000300",
      context,
    );
    assert.deepEqual(refs.people, ["U0SAM"]);
    assert.deepEqual(refs.messages, ["1759161600.000300"]);
  });

  test("reads done, blocked and decision signals, and their negations", () => {
    assert.deepEqual(signalsOf("Saved cards is done! #219 merged"), ["done"]);
    assert.deepEqual(signalsOf("It's not done yet"), []);
    assert.deepEqual(signalsOf("CHK-145 is blocked on finance"), ["blocked"]);
    assert.deepEqual(signalsOf("CHK-145 is unblocked now"), []);
    assert.deepEqual(signalsOf("Decision: we go with B."), ["decision"]);
    assert.equal(decisionStatement("For CHK-142, decision: we cap retries at 8. Then DLQ."), "We cap retries at 8.");
  });

  test("scores title overlap", () => {
    assert.equal(titleSimilarity("Update the refund wording on the order page", "Refund wording on the order page"), 1);
    assert.ok(titleSimilarity("Saved cards", "Webhook retries") === 0);
  });
});

describe("the preview workspace", () => {
  test("is consistent: every account, epic, link and thread points at something real", () => {
    const accounts = { github: new Set<string>(), jira: new Set<string>(), slack: new Set<string>() };
    for (const p of snapshot.people) for (const [s, h] of Object.entries(p.accounts)) accounts[s as "github"].add(h);
    const keys = new Set(snapshot.jira!.issues.map((i) => i.key));
    for (const pr of snapshot.github!.pullRequests) {
      assert.ok(accounts.github.has(pr.author), pr.author);
      for (const r of pr.reviews) assert.ok(accounts.github.has(r.reviewer), r.reviewer);
      assert.ok(snapshot.github!.repositories.some((r) => r.name === pr.repo), pr.repo);
    }
    for (const i of snapshot.jira!.issues) {
      if (i.assignee) assert.ok(accounts.jira.has(i.assignee), i.assignee);
      assert.ok(accounts.jira.has(i.reporter), i.reporter);
      if (i.epic) assert.ok(keys.has(i.epic), i.epic);
      for (const l of i.links ?? []) assert.ok(keys.has(l.key), l.key);
    }
    const ts = new Set(snapshot.slack!.messages.map((m) => m.ts));
    assert.equal(ts.size, snapshot.slack!.messages.length, "message ts values are unique");
    for (const m of snapshot.slack!.messages) {
      assert.ok(accounts.slack.has(m.user), m.user);
      if (m.threadTs) assert.ok(ts.has(m.threadTs), m.threadTs);
      assert.ok(snapshot.slack!.channels.some((c) => c.id === m.channel), m.channel);
    }
  });

  test("rebasing moves the anchor to now and keeps gaps", () => {
    assert.equal(workspace.anchor, new Date(NOW).toISOString());
    const before = Date.parse(snapshot.anchor) - Date.parse(snapshot.slack!.messages[0].at);
    const after = NOW - Date.parse(workspace.slack!.messages[0].at);
    assert.equal(after, before);
    assert.equal(ago(new Date(NOW - 4 * 3600_000).toISOString(), NOW), "4h ago");
  });

  test("links pull requests to tickets by title and branch", () => {
    assert.deepEqual(model.issueChanges.get("CHK-131"), ["checkout-web#219"]);
    assert.equal(model.issueChanges.get("CHK-151"), undefined, "the refund PR doesn't name its ticket");
  });

  test("maps status names to stages", () => {
    assert.equal(stageOf("In Development"), "in_progress");
    assert.equal(stageOf("In Progress"), "in_progress");
    assert.equal(stageOf("In Review"), "in_review");
    assert.equal(stageOf("To Do"), "todo");
    assert.equal(stageOf("Done"), "done");
  });
});

describe("insights", () => {
  test("finds exactly the planned ones", () => {
    assert.deepEqual([...byId.keys()].sort(), [
      "blocker:CHK-145",
      "failing_checks:checkout-api#479",
      "missing_link:CHK-151",
      "stale_review:checkout-api#476",
      "status_drift:CHK-127",
      "status_drift:CHK-131",
      "status_drift:CHK-142",
      `decision:${model.decisions.find((d) => d.issues.includes("CHK-142"))!.ts}`,
    ].sort());
  });

  test("Slack says done and the PR merged, Jira says In Development", () => {
    const i = byId.get("status_drift:CHK-131")!;
    assert.match(i.title, /CHK-131 looks done, but Jira says In Development/);
    assert.deepEqual(i.evidence.map((e) => e.source), ["slack", "github", "jira"]);
    assert.equal(i.suggestion, "Move CHK-131 to Done");
    assert.deepEqual(i.actions[0].edit, { kind: "issue_status", key: "CHK-131", to: "Done" });
  });

  test("a PR is open for review, the ticket is still in development", () => {
    const i = byId.get("status_drift:CHK-142")!;
    assert.equal(i.suggestion, "Move CHK-142 to In Review");
    assert.ok(i.evidence.some((e) => e.label === "checkout-api#482"));
  });

  test("a blocker mentioned in Slack, with what it blocks", () => {
    const i = byId.get("blocker:CHK-145")!;
    assert.equal(i.severity, "high");
    assert.match(i.summary, /also blocks CHK-129/);
  });

  test("the unlinked PR is suggested as the match", () => {
    assert.equal(byId.get("missing_link:CHK-151")!.suggestion, "Add CHK-151 to the title of checkout-web#221");
  });

  test("a decision recorded on its ticket is not flagged", () => {
    const layout = model.decisions.find((d) => d.issues.includes("CHK-120"))!;
    assert.deepEqual(layout.recordedIn.map((r) => r.key), ["CHK-120"]);
    assert.ok(!byId.has(`decision:${layout.ts}`));
  });

  test("most urgent first", () => {
    const rank = { high: 0, medium: 1, low: 2 };
    for (let n = 1; n < insights.length; n++) assert.ok(rank[insights[n - 1].severity] <= rank[insights[n].severity]);
  });

  test("only use the sources the user picked", () => {
    const githubOnly = buildModel(forSources(workspace, ["github"]));
    const ids = findInsights(githubOnly, NOW).map((i) => i.id).sort();
    assert.deepEqual(ids, ["failing_checks:checkout-api#479", "stale_review:checkout-api#476"]);
    assert.deepEqual(findInsights(buildModel(forSources(workspace, ["teams"])), NOW), []);
  });
});

describe("dashboard summaries", () => {
  test("stats", () => {
    const s = stats(model, insights);
    assert.deepEqual(s.attention, { total: 8, high: 2 });
    assert.deepEqual(s.pullRequests, { open: 7, awaitingReview: 3, drafts: 2 });
    assert.deepEqual(s.inFlight, { total: 10, inReview: 5 });
    assert.deepEqual(s.conversations, { linked: 25, total: 29 });
  });

  test("work in flight carries the PR, the last Slack mention and the insight count", () => {
    const rows = workInFlight(model, insights);
    const saved = rows.find((r) => r.key === "CHK-131")!;
    assert.equal(saved.change?.id, "checkout-web#219");
    assert.equal(saved.change?.state, "merged");
    assert.equal(saved.lastMention?.channel, "checkout-dev");
    assert.equal(saved.insights, 1);
    assert.ok(!rows.some((r) => r.type === "Epic" || r.stage === "done" || r.stage === "todo"));
  });

  test("activity is newest first", () => {
    const items = activity(model, 50);
    for (let n = 1; n < items.length; n++) assert.ok(items[n - 1].at >= items[n].at);
  });
});

describe("knowledge graph", () => {
  const graph = buildGraph(model, NOW);
  const types = new Map(graph.nodes.map((n) => [n.id, n.type]));

  // services/pipeline/ontology/edges.py: the only endpoint pairs allowed.
  const allowed: Record<EdgeType, Array<[NodeType, NodeType]>> = {
    AUTHORED: (["WorkItem", "Change", "Message", "Decision"] as const).map((t) => ["Person", t]),
    ASSIGNED_TO: [["Person", "WorkItem"], ["Person", "Change"]],
    REVIEWED: [["Person", "Change"]],
    MEMBER_OF: [["Person", "Container"]],
    OWNS: [["Person", "Module"]],
    RESOLVES_TO: [["Identity", "Person"]],
    CONTAINS: (["WorkItem", "Change", "Conversation", "Module"] as const).map((t) => ["Container", t]),
    PART_OF: [["Message", "Conversation"]],
    REPLIES_TO: [["Message", "Message"]],
    REFERENCES: (["Message", "WorkItem", "Change", "Decision"] as const).flatMap((s) =>
      (["WorkItem", "Change", "Person", "Module", "Decision", "Container"] as const).map((t): [NodeType, NodeType] => [s, t]),
    ),
    RESOLVES: [["Change", "WorkItem"]],
    RELATES_TO: [["WorkItem", "WorkItem"]],
    TOUCHES: [["Change", "Module"]],
    DECIDED_IN: (["Message", "Conversation", "Change", "WorkItem"] as const).map((t) => ["Decision", t]),
    SUPERSEDES: [["Decision", "Decision"]],
    AFFECTS: (["Module", "Container", "WorkItem"] as const).map((t) => ["Decision", t]),
  };

  test("every edge is one the ontology allows", () => {
    for (const e of graph.edges) {
      const pair = [types.get(e.source), types.get(e.target)];
      assert.ok(
        allowed[e.type].some(([s, t]) => s === pair[0] && t === pair[1]),
        `${e.type} ${pair.join(" -> ")} (${e.id})`,
      );
    }
  });

  test("ids are unique and every insight points at a node", () => {
    assert.equal(new Set(graph.nodes.map((n) => n.id)).size, graph.nodes.length);
    for (const i of insights) {
      assert.ok(types.has(i.subject), i.subject);
      for (const e of i.evidence) assert.ok(types.has(e.nodeId), e.nodeId);
    }
  });

  test("source-backed nodes live in <tenant>_<source> subgraphs", () => {
    for (const n of graph.nodes) {
      if (n.source) assert.equal(n.subgraph, `kestrel_${n.source}`);
      if (["Person", "Decision", "Module"].includes(n.type)) assert.equal(n.source, undefined);
    }
  });

  test("one person, three accounts", () => {
    const diego = graph.edges.filter((e) => e.type === "RESOLVES_TO" && e.target === "person:diego");
    assert.equal(diego.length, 3);
  });

  test("the Slack done message, the merged PR and the ticket are connected", () => {
    const has = (s: string, type: EdgeType, t: string) => graph.edges.some((e) => e.source === s && e.type === type && e.target === t);
    const done = byId.get("status_drift:CHK-131")!.evidence[0].nodeId;
    assert.ok(has(done, "REFERENCES", "workitem:jira:CHK-131"));
    assert.ok(has(done, "REFERENCES", "change:github:checkout-web#219"));
    assert.ok(has("change:github:checkout-web#219", "RESOLVES", "workitem:jira:CHK-131"));
  });
});

describe("sources", () => {
  test("a selection is checked and normalized", () => {
    assert.deepEqual(parseSources(["slack", "github", "slack"]), { ok: true, value: ["github", "slack"] });
    assert.equal(parseSources([]).ok, false);
    assert.equal(parseSources(["github", "nope"]).ok, false);
    assert.equal(parseSources("github").ok, false);
    assert.deepEqual(SOURCE_IDS, ["github", "gitlab", "jira", "slack", "teams"]);
  });

  test("saved selections are read from arrays, JSON text or nothing", () => {
    assert.deepEqual(userSources({ sources: ["jira"] }), ["jira"]);
    assert.deepEqual(userSources({ sources: '["slack","jira"]' }), ["jira", "slack"]);
    assert.deepEqual(userSources({ sources: null }), []);
    assert.deepEqual(userSources({}), []);
  });
});

describe("acting on insights", () => {
  const me = { id: "u1", name: "Payton Henry", email: "payton@example.com" };
  let n = 0;
  const run = (ws: Workspace, ...bodies: EditBody[]) =>
    applyEdits(ws, bodies.map((b): Edit => ({ ...b, id: `e${++n}`, at: new Date(NOW + n * 1000).toISOString() })), me);
  const idsAfter = (ws: Workspace) => new Set(findInsights(buildModel(ws), NOW + 60_000).map((i) => i.id));

  test("every action except a nudge resolves its insight, and nothing else changes", () => {
    for (const insight of insights) {
      assert.ok(insight.actions.length > 0, `${insight.id} has an action`);
      const after = idsAfter(run(workspace, insight.actions[0].edit));
      if (insight.kind === "stale_review") {
        assert.ok(after.has(insight.id), "a nudge asks; it doesn't review");
      } else {
        assert.ok(!after.has(insight.id), `${insight.actions[0].label} resolves ${insight.id}`);
      }
      for (const other of insights) {
        if (other.id !== insight.id && other.subject !== insight.subject) assert.ok(after.has(other.id), `${other.id} is untouched`);
      }
    }
  });

  test("a status change is recorded in the ticket's history, by the user", () => {
    const ws = run(workspace, { kind: "issue_status", key: "CHK-131", to: "Done" });
    const issue = ws.jira!.issues.find((i) => i.key === "CHK-131")!;
    assert.equal(issue.status, "Done");
    assert.deepEqual(issue.history.at(-1)!.to, "Done");
    assert.equal(buildModel(ws).byAccount.jira.get(issue.history.at(-1)!.by)!.name, "Payton Henry");
  });

  test("recording a decision links the comment to the Slack message", () => {
    const decision = insights.find((i) => i.kind === "decision")!;
    const model2 = buildModel(run(workspace, decision.actions[0].edit));
    const d = model2.decisions.find((x) => decision.subject.endsWith(x.ts))!;
    assert.deepEqual(d.recordedIn.map((r) => r.key), ["CHK-142"]);
  });

  test("a nudge is a Slack message mentioning the reviewer and the PR", () => {
    const stale = insights.find((i) => i.kind === "stale_review")!;
    const ws = run(workspace, stale.actions[0].edit);
    const posted = ws.slack!.messages.at(-1)!;
    assert.match(posted.text, /<@U0NADIA> could you review checkout-api#476/);
    assert.ok(buildModel(ws).messageRefs.get(posted.ts)!.changes.includes("checkout-api#476"));
  });

  test("no edits, same object; edits never touch the snapshot they start from", () => {
    assert.equal(applyEdits(workspace, [], me), workspace);
    const before = JSON.stringify(workspace);
    run(workspace, { kind: "issue_flag", key: "CHK-145", flagged: true });
    assert.equal(JSON.stringify(workspace), before);
  });

  test("edits for a source the user dropped are skipped", () => {
    const slackOnly = forSources(workspace, ["slack"]);
    const ws = run(slackOnly, { kind: "issue_status", key: "CHK-131", to: "Done" }, { kind: "pr_checks", change: "checkout-api#479", checks: "passing" });
    assert.equal(ws.jira, undefined);
    assert.equal(ws.github, undefined);
  });
});

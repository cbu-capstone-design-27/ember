// EMBER-125 acceptance: the dashboard reads the active organization's records
// from `source_records`, through one read-only module. The record readers run
// anywhere; the queries run on Postgres when TEST_POSTGRES_URL is set, with the
// ingestion migrations applied (CI's web job does both).
// Run: TEST_POSTGRES_URL=postgres://... npm test

import assert from "node:assert/strict";
import { after, before, describe, test } from "node:test";
import pg from "pg";
import {
  ACTIVITY_LIMIT,
  describeRecord,
  loadDashboard,
  plainSlack,
  safeUrl,
  type Queryable,
} from "../src/lib/source-records.ts";

describe("reading records", () => {
  test("a GitHub pull request, with its state and link", () => {
    const record = {
      payload: {
        repository: { full_name: "acme/web" },
        sender: { login: "bot" },
        pull_request: { number: 42, title: "Add saved cards", state: "closed", merged: true, user: { login: "ana" }, html_url: "https://github.com/acme/web/pull/42" },
      },
    };
    assert.deepEqual(describeRecord("github", "pull_request", "acme/web#42", record), {
      label: "Pull request",
      title: "Add saved cards",
      target: "acme/web#42",
      status: "Merged",
      actor: "ana",
      excerpt: null,
      url: "https://github.com/acme/web/pull/42",
    });
  });

  test("a GitHub review and a comment point at the pull request they are on", () => {
    const payload = {
      repository: { full_name: "acme/web" },
      pull_request: { number: 42, title: "Add saved cards" },
      issue: { number: 7, title: "Refund copy" },
      review: { state: "changes_requested", body: "Needs a test\nsecond line", user: { login: "raj" }, html_url: "https://github.com/acme/web/pull/42#r1" },
      comment: { body: "Same here", user: { login: "ana" }, html_url: "https://github.com/acme/web/issues/7#c1" },
    };
    const review = describeRecord("github", "pull_request_review", "acme/web/review/1", { payload });
    assert.equal(review?.status, "Changes requested");
    assert.equal(review?.target, "acme/web#42");
    assert.equal(review?.excerpt, "Needs a test");
    const comment = describeRecord("github", "issue_comment", "acme/web/issue-comment/1", { payload });
    assert.equal(comment?.title, "Refund copy");
    assert.equal(comment?.target, "acme/web#7");
    assert.equal(comment?.url, "https://github.com/acme/web/issues/7#c1");
  });

  test("GitLab reads a webhook and a REST object the same way", () => {
    const hook = {
      payload: {
        user: { username: "mei" },
        project: { path_with_namespace: "acme/api" },
        object_attributes: { iid: 5, title: "Retry queue", state: "opened", url: "https://gitlab.com/acme/api/-/merge_requests/5" },
      },
    };
    const rest = { payload: { iid: 5, title: "Retry queue", state: "merged", author: { username: "mei" }, web_url: "https://gitlab.com/acme/api/-/merge_requests/5" } };
    assert.deepEqual(describeRecord("gitlab", "merge_request", "9!5", hook), {
      label: "Merge request",
      title: "Retry queue",
      target: "acme/api!5",
      status: "Open",
      actor: "mei",
      excerpt: null,
      url: "https://gitlab.com/acme/api/-/merge_requests/5",
    });
    const fromRest = describeRecord("gitlab", "merge_request", "9!5", rest);
    assert.equal(fromRest?.status, "Merged");
    assert.equal(fromRest?.actor, "mei");
    assert.equal(fromRest?.url, "https://gitlab.com/acme/api/-/merge_requests/5");
  });

  test("a Jira issue links to its page on the site it came from", () => {
    const record = {
      payload: {
        user: { displayName: "Sam" },
        issue: { key: "EMBER-7", self: "https://acme.atlassian.net/rest/api/2/issue/10001", fields: { summary: "Fix login", status: { name: "In Progress" } } },
      },
    };
    const item = describeRecord("jira", "issue", "10001", record);
    assert.equal(item?.url, "https://acme.atlassian.net/browse/EMBER-7");
    assert.equal(item?.status, "In Progress");
    assert.equal(item?.actor, "Sam");
  });

  test("a Jira comment finds its issue in what enrich fetched", () => {
    const record = {
      payload: { comment: { id: "55", body: "Looks good", author: { displayName: "Lee" }, self: "https://acme.atlassian.net/rest/api/2/issue/10001/comment/55" } },
      issues: { "10001": { key: "EMBER-7", fields: { summary: "Fix login" } } },
    };
    const item = describeRecord("jira", "comment", "10001/comment/55", record);
    assert.equal(item?.title, "Fix login");
    assert.equal(item?.target, "EMBER-7");
    assert.equal(item?.url, "https://acme.atlassian.net/browse/EMBER-7?focusedCommentId=55");
  });

  test("a Slack message reads as the words a person sees, linked to its channel", () => {
    const record = {
      payload: { team_id: "T1" },
      message: { channel: "C9", text: "<@U1> see <https://example.com/x|the doc> &amp; <#C2|general>" },
      channel: { id: "C9", name: "eng" },
      user: { real_name: "Ana Diaz", name: "ana", profile: { display_name: "" } },
    };
    const item = describeRecord("slack", "message", "C9/1.2", record);
    assert.equal(item?.title, "@someone see the doc & #general");
    assert.equal(item?.target, "#eng");
    assert.equal(item?.actor, "Ana Diaz");
    assert.equal(item?.url, "https://app.slack.com/client/T1/C9");
  });

  test("kinds that aren't activity, unknown sources and empty records are skipped or tolerated", () => {
    assert.equal(describeRecord("github", "commit", "acme/web@abc", { payload: {} }), null);
    assert.equal(describeRecord("slack", "user", "U1", {}), null);
    assert.equal(describeRecord("teams", "message", "1", {}), null);
    assert.equal(describeRecord("github", "pull_request", "acme/web#1", null)?.title, "acme/web#1");
    assert.equal(describeRecord("jira", "issue", "1", "not an object")?.url, null);
  });

  test("only http(s) links are kept", () => {
    assert.equal(safeUrl("javascript:alert(1)"), null);
    assert.equal(safeUrl("data:text/html,x"), null);
    assert.equal(safeUrl("not a url"), null);
    assert.equal(safeUrl(42), null);
    assert.equal(safeUrl("https://example.com/a?b=1"), "https://example.com/a?b=1");
    assert.equal(describeRecord("github", "issue", "a/b#1", { payload: { issue: { title: "t", html_url: "javascript:alert(1)" } } })?.url, null);
  });

  test("Slack markup", () => {
    assert.equal(plainSlack("<@U1|ana> hi <mailto:a@b.co|mail> <https://x.test>"), "@ana hi mail https://x.test");
  });
});

const postgresUrl = process.env.TEST_POSTGRES_URL;

if (!postgresUrl) {
  test("source_records queries skipped (set TEST_POSTGRES_URL to run them)", { skip: true }, () => {});
} else {
  describe("source_records on Postgres", () => {
    const pool = new pg.Pool({ connectionString: postgresUrl });
    // The database keeps rows between runs, so every tenant is unique to this one
    const run = Date.now().toString(36);
    const tenant = (name: string) => `t${run}-${name}`;
    const tenants = ["a", "b", "versions", "gone", "open", "window", "feed", "empty"].map(tenant);
    const NOW = new Date("2030-01-15T12:00:00Z");
    const at = (hoursAgo: number) => new Date(NOW.getTime() - hoursAgo * 3_600_000);
    let counter = 0;

    async function put(
      tenantId: string,
      source: string,
      kind: string,
      objectId: string,
      occurredAt: Date,
      record: unknown,
      version = `v${++counter}`,
    ) {
      await pool.query(
        `insert into public.source_records (source, tenant, kind, object_id, version, occurred_at, record)
         values ($1, $2, $3, $4, $5, $6, $7)`,
        [source, tenantId, kind, objectId, version, occurredAt, JSON.stringify(record)],
      );
    }

    const pr = (number: number, state: string, extra: object = {}) => ({
      payload: {
        repository: { full_name: "acme/web" },
        pull_request: { number, title: `PR ${number}`, state, html_url: `https://github.com/acme/web/pull/${number}`, ...extra },
      },
    });

    before(async () => {
      const { rows } = await pool.query("select to_regclass('public.source_records') as t");
      assert.ok(rows[0].t, "apply the ingestion migrations first (infra/k8s/apps/postgres/migrations)");
    });

    after(async () => {
      await pool.query("delete from public.source_records where tenant = any($1::text[])", [tenants]);
      await pool.end();
    });

    test("one organization can't see another's records", async () => {
      // The same object id in both tenants, and a record only B has
      await put(tenant("a"), "github", "pull_request", "acme/web#1", at(1), pr(1, "open"));
      await put(tenant("b"), "github", "pull_request", "acme/web#1", at(2), pr(1, "closed"));
      await put(tenant("b"), "slack", "message", "C1/1.0", at(3), { message: { text: "secret plans", channel: "C1" }, payload: { team_id: "T1" } });

      const a = await loadDashboard(pool, tenant("a"), { now: NOW });
      assert.deepEqual(a.activity.map((i) => i.source), ["github"]);
      assert.equal(a.summary.openPullRequests.total, 1);
      assert.equal(a.summary.messages.total, 0);
      assert.deepEqual(a.sources.map((s) => s.source), ["github"]);
      assert.ok(!JSON.stringify(a).includes("secret plans"));

      const b = await loadDashboard(pool, tenant("b"), { now: NOW });
      assert.deepEqual(b.activity.map((i) => i.source).sort(), ["github", "slack"]);
      assert.equal(b.summary.openPullRequests.total, 0);
      assert.equal(b.summary.messages.total, 1);
      assert.ok(b.activity.some((i) => i.title === "secret plans"));
    });

    test("every query is a read of source_records, filtered on the tenant", async () => {
      const seen: { sql: string; values: unknown[] }[] = [];
      const watching = {
        query: (sql: string, values: unknown[]) => {
          seen.push({ sql, values });
          return pool.query(sql, values);
        },
      } as unknown as Queryable;
      await loadDashboard(watching, tenant("a"), { now: NOW });
      assert.ok(seen.length >= 2);
      for (const { sql, values } of seen) {
        assert.match(sql.trim(), /^with\b/i, "starts as a read");
        assert.doesNotMatch(sql, /\b(insert|update|delete|truncate|alter|drop|create|grant|copy|call)\b/i);
        const tables = [...sql.matchAll(/\b(?:from|join)\s+([a-z_.]+)/gi)].map((m) => m[1]).filter((t) => t.includes("."));
        assert.ok(tables.length > 0);
        for (const table of tables) assert.equal(table, "public.source_records");
        // The tenant is the first value, and every mention of the table is filtered by it
        assert.equal(values[0], tenant("a"));
        const reads = sql.match(/public\.source_records/g)?.length ?? 0;
        const filters = sql.match(/tenant = \$1/g)?.length ?? 0;
        assert.ok(filters >= reads, `${filters} tenant filters for ${reads} reads of source_records`);
      }
    });

    test("an object shows its newest version by the source's time, not by arrival", async () => {
      const t = tenant("versions");
      // The closed version arrives first (lower id) but is the later change; the stale open one arrives after it
      await put(t, "github", "pull_request", "acme/web#1", at(1), pr(1, "closed", { merged: true }), "2030-01-15T11:00:00Z");
      await put(t, "github", "pull_request", "acme/web#1", at(5), pr(1, "open"), "2030-01-15T07:00:00Z");
      await put(t, "github", "pull_request", "acme/web#2", at(4), pr(2, "open"), "a");
      await put(t, "github", "pull_request", "acme/web#2", at(2), pr(2, "open", { title: "PR 2, renamed" }), "b");
      // A tie on occurred_at goes to the later row
      await put(t, "github", "pull_request", "acme/web#3", at(3), pr(3, "open"), "c");
      await put(t, "github", "pull_request", "acme/web#3", at(3), pr(3, "closed"), "d");

      const dash = await loadDashboard(pool, t, { now: NOW });
      assert.equal(dash.summary.openPullRequests.total, 1, "only #2 is still open");
      assert.equal(dash.sources[0].records, 3, "three objects, not six versions");
      const byTitle = new Map(dash.activity.map((i) => [i.target, i]));
      assert.equal(byTitle.get("acme/web#1")?.status, "Merged");
      assert.equal(byTitle.get("acme/web#2")?.title, "PR 2, renamed");
      assert.equal(byTitle.get("acme/web#3")?.status, "Closed");
      assert.equal(dash.activity.length, 3);
    });

    test("deleted objects drop out of the feed and the numbers", async () => {
      const t = tenant("gone");
      const comment = { payload: { repository: { full_name: "acme/web" }, issue: { number: 1, title: "x" }, comment: { body: "oops", user: { login: "ana" } } } };
      await put(t, "github", "issue_comment", "acme/web/issue-comment/1", at(5), comment);
      await put(t, "github", "issue_comment", "acme/web/issue-comment/1", at(4), { ...comment, tombstone: true }, "deleted:1");
      await put(t, "slack", "message", "C1/1.0", at(3), { message: { text: "hi" }, deleted: false });
      await put(t, "slack", "message", "C1/1.0", at(2), { message: null, deleted: true }, "deleted:2");
      await put(t, "slack", "message", "C1/2.0", at(1), { message: { text: "still here" }, deleted: false });
      await put(t, "jira", "issue", "10001", at(3), { payload: { issue: { key: "J-1", fields: { summary: "s", status: { name: "To Do" } } } } });
      await put(t, "jira", "issue", "10001", at(2), { payload: { issue: { key: "J-1", fields: { summary: "s", status: { name: "To Do" } } } }, deleted: true }, "deleted:3");

      const dash = await loadDashboard(pool, t, { now: NOW });
      assert.deepEqual(dash.activity.map((i) => i.title), ["still here"]);
      assert.equal(dash.summary.messages.total, 1);
      assert.equal(dash.summary.openIssues.total, 0);
      assert.deepEqual(dash.sources.map((s) => [s.source, s.records]), [["slack", 1]]);
    });

    test("open pull requests and issues are counted by each source's own words", async () => {
      const t = tenant("open");
      await put(t, "github", "pull_request", "acme/web#1", at(1), pr(1, "open"));
      await put(t, "github", "pull_request", "acme/web#2", at(1), pr(2, "open", { draft: true }));
      await put(t, "github", "pull_request", "acme/web#3", at(1), pr(3, "closed"));
      await put(t, "github", "issue", "acme/web#4", at(1), { payload: { issue: { number: 4, title: "i", state: "open" } } });
      await put(t, "github", "issue", "acme/web#5", at(1), { payload: { issue: { number: 5, title: "i", state: "closed" } } });
      // A pull request GitHub also sent as an issue is one pull request, not an issue too
      await put(t, "github", "issue", "acme/web#1", at(1), { payload: { issue: { number: 1, title: "i", state: "open", pull_request: {} } } });
      await put(t, "gitlab", "merge_request", "9!1", at(1), { payload: { object_attributes: { iid: 1, title: "m", state: "opened" } } });
      await put(t, "gitlab", "merge_request", "9!2", at(1), { payload: { iid: 2, title: "m", state: "merged" } });
      await put(t, "gitlab", "issue", "9#1", at(1), { payload: { object_attributes: { iid: 1, title: "g", state: "opened" } } });
      await put(t, "gitlab", "issue", "9#2", at(1), { payload: { object_attributes: { iid: 2, title: "g", state: "closed" } } });
      const jira = (id: string, category: string | null, name: string) =>
        put(t, "jira", "issue", id, at(1), { payload: { issue: { key: `J-${id}`, fields: { summary: "j", status: { name, ...(category ? { statusCategory: { key: category } } : {}) } } } } });
      await jira("1", "indeterminate", "In Progress");
      await jira("2", "new", "To Do");
      await jira("3", "done", "Done");
      await jira("4", null, "Done");
      await jira("5", null, "Backlog");

      const { summary } = await loadDashboard(pool, t, { now: NOW });
      assert.deepEqual(summary.openPullRequests, { total: 3, bySource: { github: 2, gitlab: 1 } });
      assert.deepEqual(summary.openIssues, { total: 5, bySource: { github: 1, gitlab: 1, jira: 3 } });
    });

    test("messages count only the last week", async () => {
      const t = tenant("window");
      const message = { message: { text: "m" }, deleted: false };
      await put(t, "slack", "message", "C1/1", at(1), message);
      await put(t, "slack", "message", "C1/2", at(24 * 6), message);
      await put(t, "slack", "message", "C1/3", at(24 * 8), message);
      const { summary, sources } = await loadDashboard(pool, t, { now: NOW });
      assert.deepEqual(summary.messages, { total: 2, bySource: { slack: 2 } });
      assert.equal(sources[0].records, 3, "the sources card counts everything stored");
    });

    test("the feed is the newest activity, with a source, kind and link, and a count and last activity per source", async () => {
      const t = tenant("feed");
      await put(t, "github", "pull_request", "acme/web#1", at(30), pr(1, "open"));
      await put(t, "github", "commit", "acme/web@abc", at(0.1), { payload: { id: "abc", message: "wip" } });
      await put(t, "github", "label", "acme/web/label/1", at(0.2), { payload: { label: { name: "bug" } } });
      await put(t, "jira", "issue", "10001", at(2), {
        payload: { issue: { key: "J-1", self: "https://acme.atlassian.net/rest/api/2/issue/10001", fields: { summary: "Fix", status: { name: "To Do" } } } },
      });
      await put(t, "slack", "message", "C1/9", at(1), { message: { text: "ship it", channel: "C1" }, payload: { team_id: "T1" }, channel: { name: "eng" } });
      await put(t, "slack", "user", "U1", at(50), { id: "U1" });
      for (let i = 0; i < 20; i++) await put(t, "github", "issue", `acme/web#${100 + i}`, at(10 + i), { payload: { issue: { number: 100 + i, title: `Issue ${i}`, state: "open", html_url: `https://github.com/acme/web/issues/${100 + i}` } } });

      const dash = await loadDashboard(pool, t, { now: NOW });
      assert.equal(dash.activity.length, ACTIVITY_LIMIT);
      assert.deepEqual(dash.activity.slice(0, 3).map((i) => [i.source, i.kind, i.url]), [
        ["slack", "message", "https://app.slack.com/client/T1/C1"],
        ["jira", "issue", "https://acme.atlassian.net/browse/J-1"],
        ["github", "issue", "https://github.com/acme/web/issues/100"],
      ]);
      const times = dash.activity.map((i) => i.at.getTime());
      assert.deepEqual(times, [...times].sort((x, y) => y - x), "newest first");
      assert.ok(dash.activity.every((i) => !["commit", "label", "user"].includes(i.kind)));
      assert.equal((await loadDashboard(pool, t, { now: NOW, limit: 2 })).activity.length, 2);

      const github = dash.sources.find((s) => s.source === "github");
      assert.equal(github?.records, 1 + 1 + 1 + 20, "pull request, commit, label and 20 issues");
      assert.equal(github?.lastActivity.getTime(), at(0.1).getTime());
      assert.equal(dash.sources.find((s) => s.source === "slack")?.records, 2);
      assert.equal(dash.sources.find((s) => s.source === "jira")?.lastActivity.getTime(), at(2).getTime());
    });

    test("a tenant with no records gets zeros and empty lists", async () => {
      assert.deepEqual(await loadDashboard(pool, tenant("empty"), { now: NOW }), {
        summary: { openPullRequests: { total: 0, bySource: {} }, openIssues: { total: 0, bySource: {} }, messages: { total: 0, bySource: {} } },
        sources: [],
        activity: [],
      });
    });
  });
}

import json
import unittest
from pathlib import Path

from loader.mappers.base import MapContext, Reject, Skip
from loader.mappers.github import map_github

ROOT = Path(__file__).resolve().parents[3]
CTX = MapContext("brandon-dev")
GID = "brandon-dev_github"

ISSUE = {
    "number": 42, "title": "Login redirect drops the query string", "state": "open",
    "html_url": "https://github.com/acme/widget/issues/42", "user": {"login": "octocat"},
    "body": "Repro steps.", "created_at": "2026-09-23T22:15:00Z", "assignees": [{"login": "hubot"}],
}
PULL = {
    "number": 43, "title": "Fix login redirect", "state": "closed", "merged_at": "2026-09-24T10:00:00Z",
    "html_url": "https://github.com/acme/widget/pull/43", "user": {"login": "hubot"},
    "body": "Closes #42", "head": {"ref": "fix-redirect"}, "base": {"ref": "main"},
}
COMMIT = {
    "sha": "abc1234def", "html_url": "https://github.com/acme/widget/commit/abc1234def",
    "commit": {"message": "Fix redirect\n\nKeep the query string.", "author": {"date": "2026-09-23T23:30:00Z"}},
    "author": {"login": "hubot"},
}
MILESTONE = {
    "number": 3, "title": "v1.0", "state": "open", "description": "First release",
    "html_url": "https://github.com/acme/widget/milestone/3",
}
REPOSITORY = {"full_name": "acme/widget"}


def keys(mapped, label):
    return sorted(n.key for n in mapped.nodes if n.label == label)


def edge_set(mapped):
    return {(e.type, e.src_key, e.dst_key) for e in mapped.edges}


class GithubMapperTests(unittest.TestCase):
    def test_issue(self):
        m = map_github(CTX, ISSUE)
        self.assertEqual(keys(m, "WorkItem"), ["acme/widget#42"])
        self.assertEqual(keys(m, "Container"), ["acme/widget"])
        self.assertEqual(keys(m, "Identity"), ["hubot", "octocat"])
        item = next(n for n in m.nodes if n.label == "WorkItem")
        self.assertEqual(item.group_id, GID)
        self.assertEqual(item.props["status"], "open")
        self.assertIn("Repro steps.", item.props["embed_text"])
        edges = edge_set(m)
        self.assertIn(("CONTAINS", "acme/widget", "acme/widget#42"), edges)
        self.assertIn(("AUTHORED", "github:octocat", "acme/widget#42"), edges)
        self.assertIn(("ASSIGNED_TO", "github:hubot", "acme/widget#42"), edges)

    def test_pull_request_resolves_issue(self):
        m = map_github(CTX, PULL)
        change = next(n for n in m.nodes if n.label == "Change")
        self.assertEqual(change.props["state"], "merged")
        self.assertEqual(change.props["source_branch"], "fix-redirect")
        self.assertEqual(change.props["target_branch"], "main")
        self.assertIn(("RESOLVES", "acme/widget#43", "acme/widget#42"), edge_set(m))

    def test_commit(self):
        m = map_github(CTX, COMMIT)
        self.assertEqual(keys(m, "Change"), ["acme/widget@abc1234def"])
        change = next(n for n in m.nodes if n.label == "Change")
        self.assertEqual(change.props["name"], "Fix redirect")
        self.assertIn(("AUTHORED", "github:hubot", "acme/widget@abc1234def"), edge_set(m))

    def test_issues_api_view_of_a_pull_is_skipped(self):
        dup = {**ISSUE, "number": 43, "pull_request": {"url": "x"}, "html_url": "https://github.com/acme/widget/pull/43"}
        with self.assertRaises(Skip) as ctx:
            map_github(CTX, dup)
        self.assertEqual(ctx.exception.kind, "issue_is_pull")

    def test_unknown_object_is_skipped_and_orphan_is_rejected(self):
        with self.assertRaises(Skip):
            map_github(CTX, {"foo": "bar"})
        with self.assertRaises(Reject):
            map_github(CTX, {"number": 7, "title": "Orphan"})

    def test_webhook_fixture(self):
        envelope = json.loads((ROOT / "packages/ingestion-envelope/fixtures/github.json").read_text(encoding="utf-8"))
        m = map_github(CTX, envelope["body"])
        self.assertEqual(keys(m, "WorkItem"), ["acme/widget#42"])

    def test_milestone_is_skipped_not_a_work_item(self):
        with self.assertRaises(Skip) as ctx:
            map_github(CTX, MILESTONE)
        self.assertEqual(ctx.exception.kind, "milestone")

    def test_milestone_without_url_is_still_an_issue(self):
        no_url = {k: v for k, v in MILESTONE.items() if k != "html_url"}
        with self.assertRaises(Reject):  # looks like an issue, but no repo can be derived
            map_github(CTX, no_url)

    def test_comment_webhooks_are_skipped_not_the_parent(self):
        issue_comment = {"action": "created", "issue": ISSUE, "comment": {"id": 1, "body": "me too"},
                         "repository": REPOSITORY}
        review_comment = {"action": "created", "pull_request": PULL, "comment": {"id": 2, "body": "nit"},
                          "repository": REPOSITORY}
        review = {"action": "submitted", "pull_request": PULL, "review": {"id": 3, "state": "approved"},
                  "repository": REPOSITORY}
        for body, kind in ((issue_comment, "comment"), (review_comment, "comment"), (review, "review")):
            with self.subTest(kind=kind, keys=sorted(body)):
                with self.assertRaises(Skip) as ctx:
                    map_github(CTX, body)
                self.assertEqual(ctx.exception.kind, kind)

    def test_bare_rest_comments_are_skipped(self):
        issue_comment = {"id": 1, "body": "me too", "html_url": "https://github.com/acme/widget/issues/42#issuecomment-1",
                         "issue_url": "https://api.github.com/repos/acme/widget/issues/42"}
        review_comment = {"id": 2, "body": "nit", "html_url": "https://github.com/acme/widget/pull/43#discussion_r2",
                          "pull_request_url": "https://api.github.com/repos/acme/widget/pulls/43"}
        for body in (issue_comment, review_comment):
            with self.assertRaises(Skip) as ctx:
                map_github(CTX, body)
            self.assertEqual(ctx.exception.kind, "comment")


if __name__ == "__main__":
    unittest.main()

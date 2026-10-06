import json
import unittest
from pathlib import Path

from loader.mappers.base import MapContext, Reject, Skip
from loader.mappers.jira import adf_text, issue_index, map_jira

ROOT = Path(__file__).resolve().parents[3]


def adf(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


ISSUE = {
    "id": "10204", "key": "EMBER-39", "self": "https://ember-capstone.atlassian.net/rest/api/3/issue/10204",
    "fields": {
        "summary": "Define shared payload schema", "issuetype": {"name": "Story"},
        "project": {"key": "EMBER", "name": "Ember"}, "status": {"name": "In Progress"},
        "creator": {"accountId": "acc-1", "displayName": "Reporter Example"},
        "assignee": {"accountId": "acc-2", "displayName": "Assignee Example"},
        "description": adf("One envelope for every connector."), "created": "2026-09-20T12:00:00.000+0000",
        "issuelinks": [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "EMBER-45"}}],
    },
}
COMMENT = {
    "id": "20001", "self": "https://ember-capstone.atlassian.net/rest/api/3/issue/10204/comment/20001",
    "author": {"accountId": "acc-1", "displayName": "Reporter Example"},
    "body": adf("Shipping the schema today."), "created": "2026-09-21T09:00:00.000+0000",
}
COMMENT_WEBHOOK = {
    "timestamp": 1758668948000, "webhookEvent": "comment_created",
    "issue": {"id": "10204", "key": "EMBER-39", "self": ISSUE["self"], "fields": {"summary": "Define"}},
    "comment": COMMENT,
}


def edge_set(mapped):
    return {(e.type, e.src_key, e.dst_key) for e in mapped.edges}


class JiraMapperTests(unittest.TestCase):
    def test_adf_text(self):
        self.assertEqual(adf_text(adf("hello")), "hello")
        self.assertEqual(adf_text(None), "")

    def test_issue_index(self):
        self.assertEqual(issue_index(ISSUE), ("10204", "EMBER-39"))
        self.assertIsNone(issue_index(COMMENT))

    def test_issue_index_of_comment_webhook_is_its_parent_issue(self):
        self.assertEqual(issue_index(COMMENT_WEBHOOK), ("10204", "EMBER-39"))

    def test_issue(self):
        m = map_jira(MapContext("brandon-dev"), ISSUE)
        item = next(n for n in m.nodes if n.label == "WorkItem")
        self.assertEqual(item.key, "EMBER-39")
        self.assertEqual(item.group_id, "brandon-dev_jira")
        self.assertEqual(item.props["status"], "In Progress")
        self.assertEqual(item.props["item_type"], "Story")
        self.assertEqual(item.props["url"], "https://ember-capstone.atlassian.net/browse/EMBER-39")
        self.assertIn("One envelope for every connector.", item.props["embed_text"])
        edges = edge_set(m)
        self.assertIn(("CONTAINS", "EMBER", "EMBER-39"), edges)
        self.assertIn(("AUTHORED", "jira:acc-1", "EMBER-39"), edges)
        self.assertIn(("ASSIGNED_TO", "jira:acc-2", "EMBER-39"), edges)
        link = next(e for e in m.edges if e.type == "RELATES_TO")
        self.assertEqual((link.src_key, link.dst_key, link.props), ("EMBER-39", "EMBER-45", {"link_type": "Blocks"}))

    def test_comment_needs_known_issue_for_its_edge(self):
        ctx = MapContext("brandon-dev", jira_issue_keys={"10204": "EMBER-39"})
        m = map_jira(ctx, COMMENT)
        self.assertEqual([n.key for n in m.nodes if n.label == "Message"], ["20001"])
        self.assertIn(("REFERENCES", "20001", "EMBER-39"), edge_set(m))
        self.assertIn(("AUTHORED", "jira:acc-1", "20001"), edge_set(m))

        lonely = MapContext("brandon-dev")
        m = map_jira(lonely, COMMENT)
        self.assertNotIn("REFERENCES", {e.type for e in m.edges})
        self.assertEqual(len(lonely.warnings), 1)

    def test_comment_webhook_maps_the_comment_not_the_issue(self):
        ctx = MapContext("brandon-dev", jira_issue_keys={"10204": "EMBER-39"})
        m = map_jira(ctx, COMMENT_WEBHOOK)
        self.assertEqual([n.key for n in m.nodes if n.label == "Message"], ["20001"])
        self.assertEqual([n for n in m.nodes if n.label == "WorkItem"], [])
        self.assertIn(("REFERENCES", "20001", "EMBER-39"), edge_set(m))
        self.assertEqual(ctx.warnings, [])

    def test_comment_webhook_parent_comes_from_issue_id_not_comment_self(self):
        body = {**COMMENT_WEBHOOK, "comment": {**COMMENT, "self": "https://x/rest/api/3/comment/20001"}}
        ctx = MapContext("brandon-dev", jira_issue_keys={"10204": "EMBER-39"})
        self.assertIn(("REFERENCES", "20001", "EMBER-39"), edge_set(map_jira(ctx, body)))

    def test_comment_webhook_with_unknown_issue_warns(self):
        ctx = MapContext("brandon-dev")
        m = map_jira(ctx, COMMENT_WEBHOOK)
        self.assertNotIn("REFERENCES", {e.type for e in m.edges})
        self.assertEqual(len(ctx.warnings), 1)

    def test_comment_webhook_without_comment_id_is_rejected(self):
        body = {**COMMENT_WEBHOOK, "comment": {k: v for k, v in COMMENT.items() if k != "id"}}
        with self.assertRaises(Reject):
            map_jira(MapContext("brandon-dev"), body)

    def test_sibling_object_webhooks_are_skipped_with_their_kind(self):
        issue = COMMENT_WEBHOOK["issue"]
        for kind, key in (("worklog", "worklog"), ("attachment", "attachment"), ("issuelink", "issueLink")):
            with self.subTest(kind=kind):
                with self.assertRaises(Skip) as raised:
                    map_jira(MapContext("brandon-dev"), {"webhookEvent": f"{kind}_created", "issue": issue,
                                                         key: {"id": "1"}})
                self.assertEqual(raised.exception.kind, kind)
        with self.assertRaises(Skip) as raised:
            map_jira(MapContext("brandon-dev"), {"webhookEvent": "issuelink_created", "issueLink": {"id": "1"}})
        self.assertEqual(raised.exception.kind, "issuelink")

    def test_other_objects_are_skipped(self):
        with self.assertRaises(Skip):
            map_jira(MapContext("brandon-dev"), {"key": "EMBER", "self": "https://x/rest/api/3/project/1"})

    def test_issue_without_key_is_not_an_issue(self):
        with self.assertRaises(Skip):
            map_jira(MapContext("brandon-dev"), {"fields": {}, "key": "nope"})

    def test_comment_without_id_is_rejected(self):
        bad = {k: v for k, v in COMMENT.items() if k != "id"}
        with self.assertRaises(Reject):
            map_jira(MapContext("brandon-dev"), bad)

    def test_webhook_fixture(self):
        envelope = json.loads((ROOT / "packages/ingestion-envelope/fixtures/jira.json").read_text(encoding="utf-8"))
        m = map_jira(MapContext("brandon-dev"), envelope["body"])
        self.assertEqual([n.key for n in m.nodes if n.label == "WorkItem"], ["EMBER-39"])


if __name__ == "__main__":
    unittest.main()

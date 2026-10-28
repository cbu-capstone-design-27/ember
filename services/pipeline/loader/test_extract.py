"""extract's record -> episode mapping and the writers' ordering rule, without a database (EMBER-85).
The workers themselves run against Postgres in test_pg_load.py."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from loader.extract import EXTRACTOR_VERSION, SourceRow, extract, graph_records
from loader.mappers import Reject, Skip
from loader.memory_writer import MemoryWriter
from loader.records import EdgeRec, NodeRec, ordering_time
from loader.workers import _dedupe

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
ISSUE = {"id": "10001", "key": "PHO-1", "self": "https://photon.example/rest/api/3/issue/10001",
         "fields": {"summary": "Launcher opens slowly", "project": {"key": "PHO", "name": "Photon"},
                    "status": {"name": "To Do"}, "issuetype": {"name": "Bug"},
                    "creator": {"accountId": "acc-1", "displayName": "Maya Okafor"}}}
COMMENT = {"id": "20001", "self": "https://photon.example/rest/api/3/issue/10001/comment/20001",
           "body": "Reproduced on macOS 15", "author": {"accountId": "acc-2", "displayName": "Daniel Reyes"}}
MESSAGE = {"type": "message", "channel": "C1", "ts": "1789353000.000100", "user": "U1", "text": "Ship it"}


def row(source, kind, object_id, record, *, tenant="photon", version="v1", at=T0, id_=1):
    return SourceRow(id_, source, tenant, kind, object_id, version, at, record)


async def no_keys(tenant, issue_id):
    return None


def run(coro):
    return asyncio.run(coro)


def labels(draft):
    return sorted((n["label"], n["key"]) for n in draft.attrs["nodes"])


class ExtractTests(unittest.TestCase):
    def test_jira_issue_becomes_one_episode_with_its_subgraph_nodes(self):
        draft = run(extract(row("jira", "issue", "10001", {"payload": {"webhookEvent": "jira:issue_updated",
                                                                       "issue": ISSUE}}), no_keys))
        self.assertEqual(draft.key, "photon:jira:issue:10001@v1")
        self.assertEqual((draft.tenant, draft.source, draft.kind, draft.occurred_at), ("photon", "jira", "issue", T0))
        self.assertEqual(draft.attrs["group_id"], "photon_jira")
        self.assertIn(("WorkItem", "PHO-1"), labels(draft))
        self.assertIn(("Container", "PHO"), labels(draft))
        self.assertIn("Launcher opens slowly", draft.text)
        self.assertGreaterEqual(EXTRACTOR_VERSION, 1)

    def test_jira_comment_links_its_issue_from_the_payload_or_the_stored_issues(self):
        payload = {"webhookEvent": "comment_created", "comment": COMMENT, "issue": {"id": "10001", "key": "PHO-1"}}
        draft = run(extract(row("jira", "comment", "10001/comment/20001", {"payload": payload}), no_keys))
        refs = [e for e in draft.attrs["edges"] if e["type"] == "REFERENCES"]
        self.assertEqual([(e["src_key"], e["dst_key"]) for e in refs], [("20001", "PHO-1")])

        asked = []

        async def stored(tenant, issue_id):
            asked.append((tenant, issue_id))
            return "PHO-1"

        payload = {"webhookEvent": "comment_created", "comment": COMMENT, "issue": {"id": "10001"}}
        draft = run(extract(row("jira", "comment", "10001/comment/20001", {"payload": payload}), stored))
        self.assertEqual(asked, [("photon", "10001")])
        self.assertTrue(any(e["type"] == "REFERENCES" for e in draft.attrs["edges"]))

        draft = run(extract(row("jira", "comment", "10001/comment/20001", {"payload": payload}), no_keys))
        self.assertFalse(any(e["type"] == "REFERENCES" for e in draft.attrs["edges"]))
        self.assertTrue(draft.warnings)

    def test_slack_message_in_the_seed_and_the_enrich_shapes(self):
        seed = run(extract(row("slack", "message", "C1/1789353000.000100", {"payload": MESSAGE}), no_keys))
        self.assertIn(("Message", "C1:1789353000.000100"), labels(seed))
        self.assertEqual(seed.attrs["group_id"], "photon_slack")

        edited = {k: v for k, v in MESSAGE.items() if k != "channel"} | {"text": "Ship it today"}
        record = {"payload": {"type": "event_callback", "event": {"type": "message", "subtype": "message_changed",
                                                                  "channel": "C1", "message": edited}},
                  "message": edited, "deleted": False, "user": None, "channel": None, "thread_parent": None}
        draft = run(extract(row("slack", "message", "C1/1789353000.000100", record, version="1789353100.0"), no_keys))
        message = next(n for n in draft.attrs["nodes"] if n["label"] == "Message")
        self.assertEqual(message["key"], "C1:1789353000.000100")
        self.assertEqual(message["props"]["embed_text"], "Ship it today")

        fetched_parent = {"payload": None, "message": MESSAGE, "deleted": False}
        self.assertIn(("Message", "C1:1789353000.000100"),
                      labels(run(extract(row("slack", "message", "C1/1789353000.000100", fetched_parent), no_keys))))

    def test_deletes_become_tombstones_without_content(self):
        for record in ({"payload": None, "message": None, "deleted": True},
                       {"payload": {"type": "event_callback", "event": {"subtype": "message_deleted"}},
                        "message": MESSAGE, "deleted": True}):
            draft = run(extract(row("slack", "message", "C1/1789353000.000100", record), no_keys))
            self.assertTrue(draft.attrs["deleted"])
            self.assertEqual(draft.attrs["edges"], [])
            [node] = draft.attrs["nodes"]
            self.assertEqual((node["label"], node["key"]), ("Message", "C1:1789353000.000100"))
            self.assertIs(node["props"]["deleted"], True)
            self.assertNotIn("embed_text", node["props"])

        issue = run(extract(row("jira", "issue", "10001", {"payload": {"webhookEvent": "jira:issue_deleted",
                                                                       "issue": ISSUE}, "deleted": True}), no_keys))
        self.assertEqual([(n["label"], n["key"]) for n in issue.attrs["nodes"]], [("WorkItem", "PHO-1")])
        with self.assertRaises(Skip):
            run(extract(row("slack", "channel", "C1", {"deleted": True}), no_keys))

    def test_bad_records_are_rejected(self):
        with self.assertRaises(Reject):
            run(extract(row("slack", "message", "C1/1", {"payload": None}), no_keys))
        with self.assertRaises(Reject):
            run(extract(row("slack", "message", "C1/1", {"payload": MESSAGE}, tenant="Bad_Tenant"), no_keys))
        with self.assertRaises(Reject):
            run(extract(row("jira", "issue", "1", ["not", "an", "object"]), no_keys))

    def test_attrs_round_trip_to_writer_records(self):
        draft = run(extract(row("slack", "message", "C1/1789353000.000100", {"payload": MESSAGE}), no_keys))
        nodes, edges = graph_records(draft.attrs, ordering_time(T0))
        self.assertTrue(all(n.occurred_at == "2026-10-01T12:00:00.000000Z" for n in nodes + edges))
        self.assertIn("Message", {n.label for n in nodes})
        self.assertIn("PART_OF", {e.type for e in edges})


def node(text=None, at=T0, **props):
    base = {"source": "slack", "kind": "message", "external_id": "C1:1"}
    if text is not None:
        base["embed_text"] = text
    return NodeRec("Message", "photon_slack", "C1:1", base | props, occurred_at=ordering_time(at))


class OrderingRuleTests(unittest.TestCase):
    def stored(self, writer):
        return writer.nodes[("Message", node().uuid)]

    def test_a_late_older_edit_does_not_regress_a_newer_one(self):
        writer = MemoryWriter()
        writer.write_nodes([node("v2", at=T0 + timedelta(minutes=5))])
        writer.write_nodes([node("v1", at=T0, name="first")])
        stored = self.stored(writer)
        self.assertEqual(stored["embed_text"], "v2")
        self.assertEqual(stored["name"], "first", "an older row still fills what the node lacks")
        self.assertEqual(stored["occurred_at"], ordering_time(T0 + timedelta(minutes=5)))
        writer.write_nodes([node("v3", at=T0 + timedelta(minutes=9))])
        self.assertEqual(self.stored(writer)["embed_text"], "v3")

    def test_a_late_edit_does_not_bring_back_a_delete(self):
        writer = MemoryWriter()
        writer.write_nodes([NodeRec("Message", "photon_slack", "C1:1", {"source": "slack", "kind": "message",
                                    "external_id": "C1:1", "deleted": True},
                                    occurred_at=ordering_time(T0 + timedelta(hours=1)))])
        writer.write_nodes([node("secret", at=T0)])
        stored = self.stored(writer)
        self.assertIs(stored["deleted"], True)
        self.assertNotIn("embed_text", stored)

    def test_a_delete_clears_content_and_embeddings(self):
        writer = MemoryWriter()
        writer.write_nodes([node("secret", at=T0, name="secret")])
        self.stored(writer).update(embedding=[0.1], embedding_text_hash="h")
        writer.write_nodes([NodeRec("Message", "photon_slack", "C1:1", {"source": "slack", "kind": "message",
                                    "external_id": "C1:1", "deleted": True},
                                    occurred_at=ordering_time(T0 + timedelta(hours=1)))])
        self.assertEqual(set(self.stored(writer)),
                         {"group_id", "uuid", "source", "external_id", "kind", "deleted", "occurred_at"})

    def test_an_older_delete_does_not_clear_newer_content(self):
        writer = MemoryWriter()
        writer.write_nodes([node("kept", at=T0 + timedelta(hours=1))])
        writer.write_nodes([NodeRec("Message", "photon_slack", "C1:1", {"source": "slack", "kind": "message",
                                    "external_id": "C1:1", "deleted": True}, occurred_at=ordering_time(T0))])
        self.assertEqual(self.stored(writer)["embed_text"], "kept")

    def test_edges_fill_placeholders_that_their_nodes_complete(self):
        writer = MemoryWriter()
        edge = EdgeRec("REFERENCES", "photon_jira", "Message", "20001", "WorkItem", "PHO-1",
                       occurred_at=ordering_time(T0))
        writer.write_nodes([NodeRec("Message", "photon_jira", "20001", {"source": "jira", "kind": "comment",
                                                                        "external_id": "20001"})])
        self.assertEqual(writer.write_edges([edge]), 0, "without placeholders a missing endpoint drops the edge")
        self.assertEqual(writer.write_edges([edge], placeholders=True), 1)
        work_item = writer.nodes[("WorkItem", edge.dst_uuid)]
        self.assertTrue(work_item["placeholder"])
        writer.write_nodes([NodeRec("WorkItem", "photon_jira", "PHO-1", {"source": "jira", "kind": "issue",
                                                                         "external_id": "PHO-1", "name": "x"},
                                    occurred_at=ordering_time(T0 - timedelta(days=1)))])
        self.assertNotIn("placeholder", work_item)
        self.assertEqual(work_item["name"], "x")

    def test_dedupe_drops_only_repeats_that_change_nothing(self):
        a1, a2 = node("same", at=T0), node("same", at=T0 + timedelta(seconds=1))
        b = node("other", at=T0 + timedelta(seconds=2))
        a3 = node("same", at=T0 + timedelta(seconds=3))
        kept = _dedupe([a1, a2, b, a3], key=lambda n: (n.label, n.uuid))
        self.assertEqual(kept, [a2, b, a3])


if __name__ == "__main__":
    unittest.main()

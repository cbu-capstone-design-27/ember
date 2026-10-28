"""extract for enrich:github's source_records (EMBER-107): each kind, tombstones, the parent snapshots
comments and reviews carry, and the synthetic rows of testdata_github loaded in memory in source order.
The same rows run through the Postgres workers in test_pg_load.py."""

import asyncio
import re
import unittest
from pathlib import Path

from loader import testdata_github as gh
from loader.extract import EXTRACTED, SourceRow, extract, graph_records
from loader.mappers import Reject, Skip
from loader.mappers.base import MapContext
from loader.mappers.github import RECORD_KINDS, github_tombstone, map_github_record
from loader.memory_writer import MemoryWriter
from loader.records import node_uuid, ordering_time

ENRICH = Path(__file__).resolve().parents[3] / "services" / "github-ingestion" / "enrich.py"
TENANT = "photon"
GID = f"{TENANT}_github"
# Kinds enrich:github writes that have no home in docs/graph-schema.md (docs/pipeline/neo4j-load-check.md)
NOT_MAPPED = {"ref", "release", "label", "milestone"}


async def no_keys(tenant, issue_id):
    return None


def as_row(row: tuple, id_: int = 1) -> SourceRow:
    source, kind, object_id, version, occurred_at, record = row
    return SourceRow(id_, source, TENANT, kind, object_id, version, occurred_at, record)


def draft_for(row: tuple):
    return asyncio.run(extract(as_row(row), no_keys))


def by_kind(kind: str, object_id: str | None = None) -> list[tuple]:
    return [r for r in gh.rows() if r[1] == kind and (object_id is None or r[2] == object_id)]


def nodes(draft) -> dict:
    return {(n["label"], n["key"]): n for n in draft.attrs["nodes"]}


def edges(draft) -> set:
    return {(e["type"], e["src_key"], e["dst_key"]) for e in draft.attrs["edges"]}


def load_in_source_order(rows: list[tuple], writer: MemoryWriter | None = None) -> MemoryWriter:
    """What the load worker does, one episode at a time, in (occurred_at, id) order."""
    writer = writer or MemoryWriter()
    drafts = []
    for index, row in enumerate(rows):
        if (row[0], row[1]) in EXTRACTED:
            drafts.append((row[4], index, asyncio.run(extract(as_row(row, index), no_keys))))
    for occurred_at, _, draft in sorted(drafts, key=lambda d: d[:2]):
        node_recs, edge_recs = graph_records(draft.attrs, ordering_time(occurred_at))
        writer.write_nodes(node_recs)
        writer.write_edges(edge_recs, placeholders=True)
    return writer


def stored(writer: MemoryWriter, label: str, key: str) -> dict:
    return writer.nodes[(label, node_uuid(GID, label, key))]


class KindCoverageTest(unittest.TestCase):
    def test_every_kind_enrich_writes_is_extracted_or_deliberately_not(self):
        text = ENRICH.read_text(encoding="utf-8")
        written = set(re.findall(r'^\s+"\w+": \("(\w+)",', text, re.M)) | set(re.findall(r'row\("(\w+)"', text))
        self.assertIn("pull_request_review_comment", written)
        self.assertEqual(written, set(RECORD_KINDS) | NOT_MAPPED, "a kind enrich:github writes is unaccounted for")
        self.assertEqual({k for s, k in EXTRACTED if s == "github"}, set(RECORD_KINDS))

    def test_kinds_without_a_schema_home_are_skipped(self):
        for kind in sorted(NOT_MAPPED):
            with self.subTest(kind=kind), self.assertRaises(Skip) as ctx:
                map_github_record(MapContext(TENANT), kind, "x", {"payload": {}})
            self.assertEqual(ctx.exception.kind, kind)
            with self.assertRaises(Skip):
                github_tombstone(GID, kind, "x", {})


class KindMappingTest(unittest.TestCase):
    def test_repository_is_the_container(self):
        draft = draft_for(by_kind("repository")[0])
        self.assertEqual(draft.attrs["group_id"], GID)
        [container] = draft.attrs["nodes"]
        self.assertEqual((container["label"], container["key"]), ("Container", gh.REPO))
        self.assertEqual(container["props"]["url"], gh.WEB)

    def test_issue(self):
        draft = draft_for(by_kind("issue", f"{gh.REPO}#1")[0])
        item = nodes(draft)[("WorkItem", f"{gh.REPO}#1")]
        self.assertEqual((item["props"]["status"], item["props"]["item_type"], item["props"]["kind"]),
                         ("open", "issue", "issue"))
        self.assertNotIn("occurred_at", item, "a row's own object takes the episode's time")
        self.assertEqual(edges(draft), {
            ("CONTAINS", gh.REPO, f"{gh.REPO}#1"), ("AUTHORED", "github:ada-fake", f"{gh.REPO}#1"),
            ("ASSIGNED_TO", "github:bo-fake", f"{gh.REPO}#1"),
            ("RESOLVES_TO", "ada-fake", "github:ada-fake"), ("RESOLVES_TO", "bo-fake", "github:bo-fake")})
        self.assertIn("Startup shows a blank window", draft.text)

    def test_pull_request_with_its_fetched_files(self):
        opened, synced = by_kind("pull_request", f"{gh.REPO}#3")[:2]
        draft = draft_for(synced)
        change = nodes(draft)[("Change", f"{gh.REPO}#3")]["props"]
        self.assertEqual((change["kind"], change["state"], change["source_branch"], change["target_branch"]),
                         ("pull_request", "open", "fast-start", "main"))
        modules = {key: n["props"] for (label, key), n in nodes(draft).items() if label == "Module"}
        self.assertEqual(modules, {f"{gh.REPO}:src/app": {"name": "src/app", "path": "src/app"},
                                   f"{gh.REPO}:.": {"name": ".", "path": "."},
                                   f"{gh.REPO}:docs": {"name": "docs", "path": "docs"}})
        self.assertEqual({e for e in edges(draft) if e[0] == "TOUCHES"},
                         {("TOUCHES", f"{gh.REPO}#3", key) for key in modules})
        self.assertEqual({e for e in edges(draft) if e[0] == "RESOLVES"},
                         {("RESOLVES", f"{gh.REPO}#3", f"{gh.REPO}#1"), ("RESOLVES", f"{gh.REPO}#3", f"{gh.REPO}#7")})
        # an action that fetched nothing has no modules; merged is the state once merged_at is set
        merged = draft_for(by_kind("pull_request", f"{gh.REPO}#3")[3])
        self.assertFalse(any(label == "Module" for label, _ in nodes(merged)))
        self.assertEqual(nodes(merged)[("Change", f"{gh.REPO}#3")]["props"]["state"], "merged")
        self.assertEqual(len(nodes(draft_for(opened))), 1 + 1 + 2 + 2)  # change, container, ada x2, src/app and .

    def test_review_with_text_is_a_message_and_every_review_a_verdict(self):
        approval, commented = by_kind("pull_request_review")[:2]
        draft = draft_for(approval)
        self.assertFalse(any(label == "Message" for label, _ in nodes(draft)), "an approval without text has no Message")
        [reviewed] = [e for e in draft.attrs["edges"] if e["type"] == "REVIEWED"]
        self.assertEqual((reviewed["src_key"], reviewed["dst_key"], reviewed["props"]),
                         ("github:bo-fake", f"{gh.REPO}#3", {"verdict": "approved"}))

        draft = draft_for(commented)
        message = nodes(draft)[("Message", f"{gh.REPO}/review/6002")]["props"]
        self.assertEqual((message["kind"], message["embed_text"]), ("review", "Looks fine, one nit."))
        self.assertIn(("REFERENCES", f"{gh.REPO}/review/6002", f"{gh.REPO}#3"), edges(draft))
        self.assertIn(("AUTHORED", "github:cy-fake", f"{gh.REPO}/review/6002"), edges(draft))
        self.assertTrue(draft.text.endswith("Looks fine, one nit."))

    def test_the_parent_a_comment_or_review_carries_keeps_its_own_ordering_time(self):
        draft = draft_for(by_kind("pull_request_review")[1])  # submitted at minute 20
        parent = nodes(draft)[("Change", f"{gh.REPO}#3")]
        self.assertEqual(parent["occurred_at"], ordering_time(gh.at(20)))
        node_recs, _ = graph_records(draft.attrs, ordering_time(gh.at(99)))
        times = {(n.label, n.key): n.occurred_at for n in node_recs}
        self.assertEqual(times[("Change", f"{gh.REPO}#3")], ordering_time(gh.at(20)))
        self.assertEqual(times[("Message", f"{gh.REPO}/review/6002")], ordering_time(gh.at(99)))

    def test_issue_comments_reference_an_issue_or_a_pull_request(self):
        on_issue = draft_for(by_kind("issue_comment", f"{gh.REPO}/issue-comment/5001")[0])
        message = nodes(on_issue)[("Message", f"{gh.REPO}/issue-comment/5001")]["props"]
        self.assertEqual((message["kind"], message["embed_text"]), ("comment", "Seeing this too."))
        self.assertIn(("REFERENCES", f"{gh.REPO}/issue-comment/5001", f"{gh.REPO}#1"), edges(on_issue))
        self.assertIn(("WorkItem", f"{gh.REPO}#1"), nodes(on_issue))

        on_old_pull = draft_for(by_kind("issue_comment", f"{gh.REPO}/issue-comment/5004")[0])
        change = nodes(on_old_pull)[("Change", f"{gh.REPO}#9")]["props"]
        self.assertEqual((change["kind"], change["state"]), ("pull_request", "merged"))
        self.assertNotIn("source_branch", change, "the issues view of a pull request has no branches")
        [ref] = [e for e in on_old_pull.attrs["edges"] if e["type"] == "REFERENCES"]
        self.assertEqual((ref["dst_label"], ref["dst_key"]), ("Change", f"{gh.REPO}#9"))

    def test_review_comment_reply_and_commit_comment(self):
        reply = draft_for(by_kind("pull_request_review_comment")[1])
        self.assertIn(("REPLIES_TO", f"{gh.REPO}/review-comment/7002", f"{gh.REPO}/review-comment/7001"), edges(reply))
        self.assertIn(("REFERENCES", f"{gh.REPO}/review-comment/7002", f"{gh.REPO}#3"), edges(reply))

        on_commit = draft_for(by_kind("commit_comment")[0])
        self.assertIn(("REFERENCES", f"{gh.REPO}/commit-comment/8001", f"{gh.REPO}@{gh.SHA['c1a']}"), edges(on_commit))
        self.assertNotIn(("Change", f"{gh.REPO}@{gh.SHA['c1a']}"), nodes(on_commit),
                         "the payload names the commit only; its own row fills it")

    def test_push_commit_keyed_by_id(self):
        draft = draft_for(by_kind("commit")[0])
        sha = gh.SHA["c1a"]
        change = nodes(draft)[("Change", f"{gh.REPO}@{sha}")]["props"]
        self.assertEqual((change["kind"], change["name"], change["state"], change["url"]),
                         ("commit", "Split startup into two steps", "committed", f"{gh.WEB}/commit/{sha}"))
        self.assertEqual({e for e in edges(draft) if e[0] in ("CONTAINS", "AUTHORED", "TOUCHES")}, {
            ("CONTAINS", gh.REPO, f"{gh.REPO}@{sha}"), ("CONTAINS", gh.REPO, f"{gh.REPO}:src/app"),
            ("AUTHORED", "github:ada-fake", f"{gh.REPO}@{sha}"), ("TOUCHES", f"{gh.REPO}@{sha}", f"{gh.REPO}:src/app")})
        self.assertNotIn("email", nodes(draft)[("Person", "github:ada-fake")]["props"], "commit emails are not stored")

    def test_compare_api_commit_shape(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        compare = {"sha": sha, "html_url": f"{gh.WEB}/commit/{sha}", "author": {"login": "cy-fake"},
                   "commit": {"message": "Bulk change\n\nfrom a 2048+ commit push", "author": {"date": gh.iso(1)}}}
        draft = draft_for(("github", "commit", f"{gh.REPO}@{sha}", sha, gh.at(1), {"payload": compare}))
        change = nodes(draft)[("Change", f"{gh.REPO}@{sha}")]["props"]
        self.assertEqual((change["name"], change["created_at"]), ("Bulk change", gh.iso(1)))
        self.assertIn(("AUTHORED", "github:cy-fake", f"{gh.REPO}@{sha}"), edges(draft))

    def test_commit_message_emails_are_removed(self):
        sha = "fedcba9876543210fedcba9876543210fedcba98"
        message = ("Fix the retry loop\n\nSigned-off-by: Ada Fake <ada@example.test>\n"
                   "Co-authored-by: Cy Fake <cy.fake+bot@users.noreply.example.test>")
        compare = {"sha": sha, "html_url": f"{gh.WEB}/commit/{sha}", "author": {"login": "cy-fake"},
                   "commit": {"message": message, "author": {"date": gh.iso(1)}}}
        draft = draft_for(("github", "commit", f"{gh.REPO}@{sha}", sha, gh.at(1), {"payload": compare}))
        text = nodes(draft)[("Change", f"{gh.REPO}@{sha}")]["props"]["embed_text"]
        self.assertNotIn("@example.test", text)
        self.assertIn("Signed-off-by: Ada Fake <email>", text)
        self.assertIn("Fix the retry loop", text)

    def test_tombstones_keep_only_the_key(self):
        cases = {("issue", f"{gh.REPO}#2"): ("WorkItem", "issue", f"{gh.REPO}#2"),
                 ("issue_comment", f"{gh.REPO}/issue-comment/5003"):
                     ("Message", "comment", f"{gh.REPO}/issue-comment/5003")}
        for (kind, object_id), (label, node_kind, key) in cases.items():
            row = [r for r in by_kind(kind, object_id) if r[5].get("tombstone")][0]
            draft = draft_for(row)
            self.assertTrue(draft.attrs["deleted"])
            self.assertEqual(draft.attrs["edges"], [])
            [node] = draft.attrs["nodes"]
            self.assertEqual((node["label"], node["key"]), (label, key))
            self.assertEqual(node["props"], {"source": "github", "kind": node_kind, "external_id": key, "deleted": True})
            self.assertNotIn("SECRET", draft.text)
        for kind in ("pull_request_review_comment", "commit_comment"):
            [node] = github_tombstone(GID, kind, f"{gh.REPO}/x/1", {}).nodes
            self.assertEqual((node.label, node.props["kind"]), ("Message", "comment"))
        [node] = github_tombstone(GID, "repository", str(gh.REPO_ID), {"repository": {"full_name": gh.REPO}}).nodes
        self.assertEqual((node.label, node.key), ("Container", gh.REPO))
        with self.assertRaises(Reject):
            github_tombstone(GID, "repository", str(gh.REPO_ID), {})

    def test_bad_rows_are_rejected(self):
        for kind, record in (("issue", {"payload": None}), ("issue", {"payload": {"issue": {"number": 1}}}),
                             ("pull_request_review", {"payload": {"repository": {"full_name": gh.REPO}}}),
                             ("issue_comment", {"payload": {"repository": {"full_name": gh.REPO}, "comment": {}}})):
            with self.subTest(kind=kind), self.assertRaises(Reject):
                draft_for(("github", kind, "x", "v", gh.T0, record))


class InMemoryLoadTest(unittest.TestCase):
    """The fixture rows loaded the way the load worker does, without Postgres: counts and the rules."""

    def setUp(self):
        self.writer = load_in_source_order(gh.rows())

    def test_counts_match_the_hand_counted_expectation(self):
        facts = self.writer.read_facts([GID])
        self.assertEqual(facts.node_counts, gh.EXPECTED["nodes"])
        self.assertEqual({f"{s} {t} {d}": n for (s, t, d), n in facts.relationships.items()},
                         gh.EXPECTED["relationships"])
        self.assertEqual(facts.placeholders, gh.EXPECTED["placeholders"])
        self.assertEqual(facts.missing_required, {})
        self.assertEqual({p["group_id"] for p in self.writer.nodes.values()}, {GID})

    def test_latest_version_wins_by_ordering_value(self):
        pull = stored(self.writer, "Change", f"{gh.REPO}#3")
        self.assertEqual((pull["name"], pull["state"]), ("Prism: faster startup", "merged"))
        self.assertEqual(stored(self.writer, "WorkItem", f"{gh.REPO}#1")["status"], "closed",
                         "an older snapshot in a comment did not reopen the issue")
        self.assertEqual(stored(self.writer, "Message", f"{gh.REPO}/issue-comment/5002")["embed_text"],
                         "Ready for review now.")
        old = stored(self.writer, "Change", f"{gh.REPO}#9")
        self.assertEqual((old["state"], old.get("placeholder")), ("merged", None))

    def test_deletes_clear_content_and_late_older_rows_do_not_bring_it_back(self):
        load_in_source_order([gh.late_comment_edit_row(), gh.late_pull_request_row()], self.writer)
        for label, key in (("WorkItem", f"{gh.REPO}#2"), ("Message", f"{gh.REPO}/issue-comment/5003")):
            props = stored(self.writer, label, key)
            self.assertIs(props["deleted"], True)
            self.assertEqual(set(props), {"group_id", "uuid", "source", "external_id", "kind", "deleted", "occurred_at"})
        pull = stored(self.writer, "Change", f"{gh.REPO}#3")
        self.assertEqual((pull["name"], pull["state"]), ("Prism: faster startup", "merged"))


if __name__ == "__main__":
    unittest.main()

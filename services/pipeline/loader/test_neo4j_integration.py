"""Runs against a real Neo4j. Skipped unless NEO4J_TEST_URI is set.

  NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=... \
    python -m unittest loader.test_neo4j_integration -v
Uses the throwaway tenant 'it-loader' and removes only that tenant's data.
"""

import os
import tempfile
import unittest
from pathlib import Path

from loader.neo4j_writer import Neo4jWriter
from loader.records import node_uuid
from ontology import tenant_subgraphs

URI = os.environ.get("NEO4J_TEST_URI")
TENANT = "it-loader"
OTHER = "it-loader-other"


@unittest.skipUnless(URI, "NEO4J_TEST_URI not set")
class Neo4jIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.writer = Neo4jWriter(URI, os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
        self.writer.reset(tenant_subgraphs(TENANT) + tenant_subgraphs(OTHER))

    def tearDown(self):
        self.writer.reset(tenant_subgraphs(TENANT) + tenant_subgraphs(OTHER))
        self.writer.close()

    def test_load_is_clean_and_idempotent(self):
        from loader.run import run
        from loader.testdata import write_sample

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            write_sample(root, include_bad=False)
            first = run(TENANT, root, self.writer, Path(tmp) / "rejects.jsonl")
            self.assertEqual(first.problems, [], first.render())
            self.assertEqual(first.exit_code, 0, first.render())
            second = run(TENANT, root, self.writer, Path(tmp) / "rejects.jsonl")
        self.assertEqual(first.node_counts, second.node_counts)


    def _external_ids(self, tenant):
        rows = self.writer._run("MATCH (n) WHERE n.group_id IN $gids RETURN n.external_id AS e, n.uuid AS u",
                                gids=tenant_subgraphs(tenant))
        return {(r["e"], r["u"]) for r in rows}

    def test_reset_removes_the_tenants_stale_nodes_and_keeps_other_tenants(self):
        from loader.run import run
        from loader.testdata import write_sample

        with tempfile.TemporaryDirectory() as tmp:
            root, trimmed, rejects = Path(tmp) / "raw", Path(tmp) / "trimmed", Path(tmp) / "rejects.jsonl"
            write_sample(root, include_bad=False)
            write_sample(trimmed, include_bad=False)  # same sample without the commit record
            github = trimmed / "github" / "github.jsonl"
            kept = [line for line in github.read_text(encoding="utf-8").splitlines() if '"sha"' not in line]
            github.write_text("\n".join(kept) + "\n", encoding="utf-8")

            run(TENANT, root, self.writer, rejects)
            run(OTHER, root, self.writer, rejects)
            mine, theirs = self._external_ids(TENANT), self._external_ids(OTHER)
            self.assertTrue(any(e and e.endswith("abc1234def") for e, _ in mine))

            report = run(TENANT, trimmed, self.writer, rejects, reset=True)
            self.assertEqual(report.problems, [], report.render())
            after = self._external_ids(TENANT)
            self.assertFalse(any(e and e.endswith("abc1234def") for e, _ in after))  # stale node removed
            self.assertTrue(after)
            self.assertEqual(self._external_ids(OTHER), theirs)  # the other tenant is untouched


    def test_node_with_the_same_schema_key_but_another_uuid_is_adopted(self):
        from loader.run import run
        from loader.testdata import write_sample

        group = tenant_subgraphs(TENANT)[0]  # the github subgraph
        self.writer._run("CREATE (:WorkItem {group_id: $g, source: 'github', external_id: 'acme/widget#42', "
                         "kind: 'issue', uuid: 'stale-uuid'})", g=group)
        self.writer._run("CREATE (:Container {group_id: $g, source: 'github', external_id: 'acme/widget', "
                         "kind: 'repository'})", g=group)  # exists with no uuid at all
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            write_sample(root, include_bad=False)
            report = run(TENANT, root, self.writer, Path(tmp) / "rejects.jsonl")
        self.assertEqual(report.problems, [], report.render())
        for label, external_id in (("WorkItem", "acme/widget#42"), ("Container", "acme/widget")):
            rows = self.writer._run(f"MATCH (n:{label}) WHERE n.group_id = $g AND n.external_id = $e "
                                    "RETURN n.uuid AS u, n.source AS s", g=group, e=external_id)
            self.assertEqual(len(rows), 1, (label, rows))
            self.assertEqual(rows[0]["u"], node_uuid(group, label, external_id))
        # edges still attach to the adopted node
        linked = self.writer._run("MATCH (:Container {group_id: $g, external_id: 'acme/widget'})-[:CONTAINS]->"
                                  "(w:WorkItem {external_id: 'acme/widget#42'}) RETURN count(w) AS c", g=group)
        self.assertEqual(linked[0]["c"], 1)


if __name__ == "__main__":
    unittest.main()

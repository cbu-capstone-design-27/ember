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
from ontology import tenant_subgraphs

URI = os.environ.get("NEO4J_TEST_URI")
TENANT = "it-loader"


@unittest.skipUnless(URI, "NEO4J_TEST_URI not set")
class Neo4jIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.writer = Neo4jWriter(URI, os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
        self.writer.reset(tenant_subgraphs(TENANT))

    def tearDown(self):
        self.writer.reset(tenant_subgraphs(TENANT))
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


if __name__ == "__main__":
    unittest.main()

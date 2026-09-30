"""Contract tests for the graph ontology. Run: python3 services/pipeline/ontology/test_ontology.py

Needs pydantic (pip install "pydantic>=2,<3"). Runs in CI as the `pipeline` job.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "pipeline"))

from ontology import (  # noqa: E402
    EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, EXPECTED, SOURCES,
    EmbeddingIdentity, EmbeddingMismatch, check_embedding_identity,
    parse_subgraph_id, subgraph_id, tenant_subgraphs,
)

FIXTURES = ROOT / "packages" / "ingestion-envelope" / "fixtures"
LAUNCH_SOURCES = {"github", "gitlab", "jira", "slack", "teams"}
RESERVED_NODE_FIELDS = {
    "uuid", "name", "group_id", "labels", "created_at", "name_embedding", "summary", "attributes",
}
RESERVED_EDGE_FIELDS = {
    "uuid", "source_node_uuid", "target_node_uuid", "created_at", "name", "group_id", "fact",
    "fact_embedding", "episodes", "expired_at", "valid_at", "invalid_at", "reference_time",
    "attributes",
}


class OntologyTests(unittest.TestCase):
    def test_no_reserved_field_names(self):
        for name, cls in ENTITY_TYPES.items():
            self.assertFalse(RESERVED_NODE_FIELDS & set(cls.model_fields), name)
        for name, cls in EDGE_TYPES.items():
            self.assertFalse(RESERVED_EDGE_FIELDS & set(cls.model_fields), name)

    def test_types_have_descriptions(self):
        for name, cls in {**ENTITY_TYPES, **EDGE_TYPES}.items():
            self.assertTrue((cls.__doc__ or "").strip(), f"{name} needs a docstring")

    def test_edge_map_refers_to_known_types(self):
        for (src, dst), names in EDGE_TYPE_MAP.items():
            self.assertIn(src, ENTITY_TYPES)
            self.assertIn(dst, ENTITY_TYPES)
            for n in names:
                self.assertIn(n, EDGE_TYPES)
        used = {n for names in EDGE_TYPE_MAP.values() for n in names}
        self.assertEqual(used, set(EDGE_TYPES), "every edge type must be allowed somewhere")

    def test_launch_sources_present_and_kinds_map_to_core_types(self):
        self.assertEqual(set(SOURCES), LAUNCH_SOURCES)
        for source in SOURCES.values():
            for kind, cls in source.kinds.items():
                self.assertIn(cls.__name__, ENTITY_TYPES, f"{source.name}.{kind}")

    def test_every_fixture_classifies_to_a_declared_kind(self):
        for name in LAUNCH_SOURCES:
            envelope = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
            source = SOURCES[envelope["type"]]
            kind = source.kind_of(envelope["body"])
            self.assertIn(kind, source.kinds, f"{name} fixture classified as {kind!r}")

    def test_specific_fixture_kinds(self):
        expect = {"github": "issue", "gitlab": "merge_request", "jira": "issue",
                  "slack": "message", "teams": "message"}
        for name, kind in expect.items():
            envelope = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
            self.assertEqual(SOURCES[name].kind_of(envelope["body"]), kind)

    def test_subgraph_ids(self):
        self.assertEqual(subgraph_id("acme", "github"), "acme_github")
        self.assertEqual(parse_subgraph_id("cbu-ember_slack"), ("cbu-ember", "slack"))
        self.assertEqual(len(tenant_subgraphs("acme")), len(LAUNCH_SOURCES))
        for gid in tenant_subgraphs("acme"):
            self.assertRegex(gid, r"^[A-Za-z0-9_-]+$")
        for tenant, source in [("acme_co", "github"), ("Acme", "github"), ("acme", "discord"), ("", "jira")]:
            with self.assertRaises(ValueError):
                subgraph_id(tenant, source)

    def test_embedding_identity_check(self):
        check_embedding_identity(EXPECTED)
        other = EmbeddingIdentity("some/other-model", EXPECTED.revision, 768, EXPECTED.query_template)
        with self.assertRaises(EmbeddingMismatch) as ctx:
            check_embedding_identity(other)
        self.assertIn("full rebuild", str(ctx.exception))
        self.assertIn("dimension", str(ctx.exception))

    def test_cypher_statements_are_idempotent(self):
        text = (Path(__file__).parent / "schema.cypher").read_text(encoding="utf-8")
        statements = [s for s in text.split(";") if "CREATE CONSTRAINT" in s and not s.strip().startswith("//")]
        self.assertGreater(len(statements), 0)
        for s in statements:
            self.assertIn("IF NOT EXISTS", s)


if __name__ == "__main__":
    unittest.main()

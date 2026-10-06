import unittest

from loader.conform import validate_mapped
from loader.mappers.base import Mapped, Reject
from loader.records import EdgeRec, NodeRec

G = "t_github"


def good_item():
    return NodeRec("WorkItem", G, "a#1", {"source": "github", "kind": "issue", "external_id": "a#1"})


class ConformTests(unittest.TestCase):
    def test_valid_records_pass(self):
        edge = EdgeRec("CONTAINS", G, "Container", "a", "WorkItem", "a#1")
        validate_mapped(Mapped([good_item()], [edge]), "github")

    def test_unknown_label(self):
        with self.assertRaisesRegex(Reject, "unknown node label"):
            validate_mapped(Mapped([NodeRec("Ticket", G, "x", {})]), "github")

    def test_missing_required_source_field(self):
        bad = NodeRec("WorkItem", G, "a#1", {"source": "github", "external_id": "a#1"})
        with self.assertRaisesRegex(Reject, "WorkItem"):
            validate_mapped(Mapped([bad]), "github")

    def test_non_string_external_id(self):
        bad = NodeRec("WorkItem", G, "a#1", {"source": "github", "kind": "issue", "external_id": 5})
        with self.assertRaises(Reject):
            validate_mapped(Mapped([bad]), "github")

    def test_source_must_match_the_envelope_type(self):
        with self.assertRaisesRegex(Reject, "source"):
            validate_mapped(Mapped([good_item()]), "jira")

    def test_edge_pair_not_allowed(self):
        edge = EdgeRec("AUTHORED", G, "WorkItem", "a#1", "Person", "p")
        with self.assertRaisesRegex(Reject, "AUTHORED not allowed from WorkItem to Person"):
            validate_mapped(Mapped([good_item()], [edge]), "github")

    def test_unknown_edge_type(self):
        edge = EdgeRec("LIKES", G, "Person", "p", "WorkItem", "a#1")
        with self.assertRaisesRegex(Reject, "unknown relationship type"):
            validate_mapped(Mapped([], [edge]), "github")


if __name__ == "__main__":
    unittest.main()

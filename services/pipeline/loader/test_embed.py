import tempfile
import unittest
from pathlib import Path

from loader.embed import embed_graph, text_hash
from loader.memory_writer import MemoryWriter
from loader.records import NodeRec
from loader.run import run
from loader.testdata import write_sample
from ontology.config import EmbeddingIdentity, EmbeddingMismatch

G = "t_github"
IDENTITY = EmbeddingIdentity("m1", "r1", 2, "{query}")


class FakeClient:
    def __init__(self):
        self.calls = []

    def embed(self, texts, **kwargs):
        self.calls.append(list(texts))
        return [[1.0, float(len(t))] for t in texts]


def node(key, text):
    return NodeRec("WorkItem", G, key, {"source": "github", "kind": "issue", "external_id": key, "embed_text": text})


class EmbedGraphTests(unittest.TestCase):
    def test_embeds_stores_the_vector_and_records_the_identity(self):
        w, c = MemoryWriter(), FakeClient()
        w.write_nodes([node("a#1", "hello"), NodeRec("Person", G, "p", {"name": "x"})])
        self.assertEqual(embed_graph(w, c, IDENTITY, [G]), 1)
        stored = next(v for (label, _), v in w.nodes.items() if label == "WorkItem")
        self.assertEqual(stored["embedding"], [1.0, 5.0])
        self.assertEqual(stored["embedding_text_hash"], text_hash("hello"))
        self.assertEqual(w.read_embedding_identity(), IDENTITY)

    def test_second_run_embeds_nothing_and_a_changed_text_embeds_again(self):
        w, c = MemoryWriter(), FakeClient()
        w.write_nodes([node("a#1", "hello"), node("a#2", "world")])
        embed_graph(w, c, IDENTITY, [G])
        self.assertEqual(embed_graph(w, c, IDENTITY, [G]), 0)
        w.write_nodes([node("a#2", "world, edited")])
        self.assertEqual(embed_graph(w, c, IDENTITY, [G]), 1)
        self.assertEqual(c.calls[-1], ["world, edited"])

    def test_a_different_model_is_refused_before_anything_is_embedded(self):
        w, c = MemoryWriter(), FakeClient()
        w.write_nodes([node("a#1", "hello")])
        embed_graph(w, c, IDENTITY, [G])
        other = EmbeddingIdentity("m2", "r1", 2, "{query}")
        w.write_nodes([node("a#3", "new text")])
        with self.assertRaises(EmbeddingMismatch):
            embed_graph(w, c, other, [G])
        self.assertEqual(len(c.calls), 1)

    def test_batches(self):
        w, c = MemoryWriter(), FakeClient()
        w.write_nodes([node(f"a#{i}", f"text {i}") for i in range(5)])
        embed_graph(w, c, IDENTITY, [G], batch=2)
        self.assertEqual([len(call) for call in c.calls], [2, 2, 1])

    def test_blank_text_is_not_a_candidate_and_never_reaches_the_client(self):
        w, c = MemoryWriter(), FakeClient()
        w.write_nodes([node("a#1", ""), node("a#2", " " + chr(10) + chr(9) + " "), node("a#3", "real")])
        self.assertEqual([cand["key"] for cand in w.embed_candidates([G])], ["a#3"])
        self.assertEqual(embed_graph(w, c, IDENTITY, [G]), 1)
        self.assertEqual(c.calls, [["real"]])

    def test_candidates_expose_what_the_benchmark_needs(self):
        w, c = MemoryWriter(), FakeClient()
        w.write_nodes([node("a#1", "hello")])
        embed_graph(w, c, IDENTITY, [G])
        (cand,) = w.embed_candidates([G])
        self.assertEqual((cand["label"], cand["group_id"], cand["key"], cand["text"], cand["vector"]),
                         ("WorkItem", G, "a#1", "hello", [1.0, 5.0]))


class RunWithEmbedderTests(unittest.TestCase):
    def test_run_reports_the_count_and_turns_embedding_errors_into_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            write_sample(root, include_bad=False)
            w, c = MemoryWriter(), FakeClient()
            report = run("brandon-dev", root, w, Path(tmp) / "r.jsonl",
                         embedder=lambda wr, gids: embed_graph(wr, c, IDENTITY, gids))
            self.assertEqual(report.exit_code, 0, report.render())
            # the sample's text-bearing nodes: GitHub issue, PR and commit, Jira issue and comment, two Slack messages
            self.assertEqual(report.embedded, 7)
            self.assertIn("embedded:", report.render())

            other = EmbeddingIdentity("m2", "r1", 2, "{query}")
            bad = run("brandon-dev", root, w, Path(tmp) / "r.jsonl",
                      embedder=lambda wr, gids: embed_graph(wr, c, other, gids))
        self.assertEqual(bad.exit_code, 1)
        self.assertTrue(any("embedding step failed" in p for p in bad.problems))
        self.assertEqual(bad.nodes_written, report.nodes_written)  # the graph was still written


if __name__ == "__main__":
    unittest.main()

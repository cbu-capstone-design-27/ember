import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.results import BenchmarkError
from benchmarks.retrieval_suite import SYSTEMS, Doc, Question, docs_from, load_questions, run_retrieval
from benchmarks.testing import BAG_IDENTITY, BagClient, bag_of_words_embed
from loader.embed import embed_graph
from loader.memory_writer import MemoryWriter
from loader.run import run
from loader.testdata import write_sample
from ontology import tenant_subgraphs

DATA = Path(__file__).parent / "data"


def embedded_sample():
    with tempfile.TemporaryDirectory() as tmp:
        write_sample(Path(tmp) / "raw", include_bad=False)
        writer = MemoryWriter()
        run("brandon-dev", Path(tmp) / "raw", writer, Path(tmp) / "r.jsonl")
    embed_graph(writer, BagClient(), BAG_IDENTITY, tenant_subgraphs("brandon-dev"))
    return writer


class QuestionFileTests(unittest.TestCase):
    def test_sample_file_loads(self):
        meta, questions = load_questions(DATA / "questions_sample.json")
        self.assertEqual(meta["dataset"], "loader sample data")
        self.assertEqual([q.id for q in questions], ["s1", "s2", "s3"])
        self.assertEqual(questions[1].answers, ("jira:EMBER-39",))

    def test_bad_files_are_rejected(self):
        cases = {
            "duplicate ids": [{"id": "a", "question": "x", "answers": ["r"]}, {"id": "a", "question": "y", "answers": ["r"]}],
            "no answers": [{"id": "a", "question": "x", "answers": []}],
            "no text": [{"id": "a", "question": " ", "answers": ["r"]}],
        }
        for name, questions in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "q.json"
                path.write_text(json.dumps({"questions": questions}), encoding="utf-8")
                with self.assertRaises(BenchmarkError):
                    load_questions(path)


class DocsTests(unittest.TestCase):
    def test_docs_use_source_prefixed_refs(self):
        docs = docs_from(embedded_sample().embed_candidates(tenant_subgraphs("brandon-dev")))
        refs = {d.ref for d in docs}
        self.assertIn("github:acme/widget#42", refs)
        self.assertIn("slack:C012AB3CD:1717000001.000200", refs)
        self.assertEqual(len(docs), 7)

    def test_missing_vectors_are_an_error(self):
        candidates = [{"group_id": "t_github", "key": "a#1", "text": "x", "vector": None}]
        with self.assertRaisesRegex(BenchmarkError, "no vector"):
            docs_from(candidates)


class RunRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.docs = docs_from(embedded_sample().embed_candidates(tenant_subgraphs("brandon-dev")))
        _, self.questions = load_questions(DATA / "questions_sample.json")

    def test_scores_and_details_for_every_system(self):
        result = run_retrieval(self.questions, self.docs, bag_of_words_embed)
        self.assertEqual(set(result["scores"]), set(SYSTEMS))
        for system in SYSTEMS:
            self.assertEqual(set(result["scores"][system]), {"recall@1", "recall@3", "recall@5", "recall@10", "mrr"})
            self.assertEqual(result["scores"][system]["recall@10"], 1.0, system)
        self.assertEqual([d["id"] for d in result["details"]], ["s1", "s2", "s3"])
        self.assertEqual(set(result["details"][0]["ranks"]), set(SYSTEMS))

    def test_answers_missing_from_the_graph_are_reported_together(self):
        questions = self.questions + [Question("x", "anything", ("github:nowhere#1", "jira:NOPE-1"))]
        with self.assertRaisesRegex(BenchmarkError, "github:nowhere#1.*jira:NOPE-1"):
            run_retrieval(questions, self.docs, bag_of_words_embed)

    def test_vector_only_finds_what_bm25_cannot(self):
        docs = [Doc("a:1", "alpha beta", [1.0, 0.0]), Doc("a:2", "gamma delta", [0.0, 1.0])]
        questions = [Question("q", "unrelated words", ("a:2",))]
        result = run_retrieval(questions, docs, lambda texts, **kw: [[0.0, 1.0] for _ in texts], ks=(1,))
        self.assertIsNone(result["details"][0]["ranks"]["bm25"])
        self.assertEqual(result["details"][0]["ranks"]["vector"], 1)
        self.assertEqual(result["details"][0]["ranks"]["hybrid"], 1)
        self.assertEqual(result["scores"]["bm25"]["recall@1"], 0.0)

    def test_queries_are_embedded_as_queries(self):
        seen = {}

        def embed(texts, **kwargs):
            seen.update(kwargs)
            return bag_of_words_embed(texts)

        run_retrieval(self.questions, self.docs, embed)
        self.assertTrue(seen["is_query"])


if __name__ == "__main__":
    unittest.main()

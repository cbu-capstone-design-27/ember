import unittest

from benchmarks.retrieval import BM25, cosine, mrr, rank_of, recall_at, rrf, tokenize, vector_rank


class TokenizeTests(unittest.TestCase):
    def test_lowercases_and_splits_on_punctuation(self):
        self.assertEqual(tokenize("Fix EMBER-39: login_redirect!"), ["fix", "ember", "39", "login", "redirect"])


class BM25Tests(unittest.TestCase):
    DOCS = {
        "a": "login redirect drops the query string",
        "b": "fix the login page colour",
        "c": "database migration for graph schema",
    }

    def test_ranks_the_best_match_first_and_skips_non_matches(self):
        bm = BM25(self.DOCS)
        self.assertEqual(bm.rank("login redirect", 10), ["a", "b"])
        self.assertEqual(bm.rank("zebra", 10), [])

    def test_rare_terms_count_more(self):
        bm = BM25({"x": "the the the migration", "y": "the the the the the login", "z": "unrelated"})
        self.assertEqual(bm.rank("the migration", 10)[0], "x")

    def test_k_limits_the_list(self):
        self.assertEqual(len(BM25(self.DOCS).rank("the", 1)), 1)


class VectorTests(unittest.TestCase):
    def test_cosine(self):
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(cosine([0, 0], [1, 1]), 0.0)

    def test_vector_rank_orders_by_similarity(self):
        vectors = {"near": [1.0, 0.1], "far": [0.0, 1.0], "mid": [1.0, 1.0]}
        self.assertEqual(vector_rank([1.0, 0.0], vectors, 2), ["near", "mid"])


class FusionAndMetricTests(unittest.TestCase):
    def test_rrf_rewards_agreement(self):
        self.assertEqual(rrf([["a", "b", "c"], ["c", "a", "d"]])[:2], ["a", "c"])

    def test_rrf_keeps_documents_found_by_only_one_system(self):
        self.assertEqual(set(rrf([["a"], ["b"]])), {"a", "b"})

    def test_rank_of(self):
        self.assertEqual(rank_of(["x", "y", "z"], {"z", "q"}), 3)
        self.assertIsNone(rank_of(["x"], {"q"}))

    def test_recall_and_mrr(self):
        ranks = [1, 3, None, 12]
        self.assertEqual(recall_at(ranks, 1), 0.25)
        self.assertEqual(recall_at(ranks, 5), 0.5)
        self.assertEqual(recall_at(ranks, 10), 0.5)
        self.assertAlmostEqual(mrr(ranks), (1 + 1 / 3 + 0 + 1 / 12) / 4)

    def test_empty_inputs_score_zero(self):
        self.assertEqual((recall_at([], 5), mrr([])), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()

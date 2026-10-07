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

    def test_length_normalization_shorter_outranks_longer_with_same_term_count(self):
        # Docs b="t t" (len 2) and a="t t w w" (len 4), query "t"
        # avg_len=3, idf_t=log(1.2)≈0.1823, b gets score≈0.2917, a gets score≈0.2343
        # If b=0 (no length norm): both score≈0.4558, tied, sorted by ref → a,b (test would fail)
        bm = BM25({"b": "t t", "a": "t t w w"})
        result = bm.rank("t", 10)
        self.assertEqual(result[0], "b")
        self.assertAlmostEqual(bm.rank("t", 10), ["b", "a"], places=5)

    def test_rare_terms_contribute_more_via_higher_idf(self):
        # Docs: x="the the the migration", y="the the the the the login", z="unrelated"
        # "migration" appears in 1 doc (rare), "login" in 1 doc (rare), "the" in 2 docs (common)
        # Query "the migration": x gets idf boost from "migration", y doesn't → x ranks higher
        bm = BM25({"x": "the the the migration", "y": "the the the the the login", "z": "unrelated"})
        result = bm.rank("the migration", 10)
        self.assertEqual(result[0], "x")

    def test_k_limits_the_list(self):
        self.assertEqual(len(BM25(self.DOCS).rank("the", 1)), 1)

    def test_single_doc_exact_score(self):
        # Docs: {"a": "x"}, query "x"
        # idf_x = log(1 + 0.5/1.5) = log(4/3) ≈ 0.287682
        # norm = 1 + 1.5 * 1 = 2.5, score = log(4/3) * 1 * 2.5 / 2.5 = log(4/3) ≈ 0.287682
        bm = BM25({"a": "x"})
        scores = {}
        for ref, tf in bm.tf.items():
            score = 0.0
            for term in ["x"]:
                freq = tf.get(term, 0)
                if freq:
                    norm = freq + bm.k1 * (1 - bm.b + bm.b * bm.length[ref] / bm.avg)
                    score += bm.idf[term] * freq * (bm.k1 + 1) / norm
            scores[ref] = score
        self.assertAlmostEqual(scores["a"], 0.287682, places=5)


class VectorTests(unittest.TestCase):
    def test_cosine(self):
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(cosine([0, 0], [1, 1]), 0.0)

    def test_cosine_raises_on_length_mismatch(self):
        with self.assertRaises(ValueError) as cm:
            cosine([1, 0], [1])
        self.assertIn("same length", str(cm.exception))

    def test_vector_rank_orders_by_similarity(self):
        vectors = {"near": [1.0, 0.1], "far": [0.0, 1.0], "mid": [1.0, 1.0]}
        self.assertEqual(vector_rank([1.0, 0.0], vectors, 2), ["near", "mid"])

    def test_vector_rank_includes_zero_scores(self):
        # vector_rank returns all docs up to k, including zero or negative scores
        vectors = {"a": [1.0, 0.0], "b": [0.0, 0.0]}
        result = vector_rank([1.0, 0.0], vectors, 10)
        self.assertEqual(set(result), {"a", "b"})


class FusionAndMetricTests(unittest.TestCase):
    def test_rrf_rewards_agreement(self):
        self.assertEqual(rrf([["a", "b", "c"], ["c", "a", "d"]])[:2], ["a", "c"])

    def test_rrf_keeps_documents_found_by_only_one_system(self):
        self.assertEqual(set(rrf([["a"], ["b"]])), {"a", "b"})

    def test_rrf_with_tie_broken_by_ref(self):
        # Both rankings have "b" at position 1 (score 1/61), "a" at position 2 (score 1/62)
        # Total: "b" gets 2/61, "a" gets 2/62, "b" > "a", but test with tied final scores
        # Ranking1: ["x", "y"], Ranking2: ["x", "y"] → both get 2/61 and 2/62 respectively
        result = rrf([["x", "y"], ["x", "y"]])
        self.assertEqual(result[:2], ["x", "y"])

    def test_rrf_c_parameter_offset(self):
        # With c=1: rank 1 contributes 1/(1+1)=0.5, rank 2 contributes 1/(1+2)=0.333...
        # {"a": [0.5], "b": [0.333]}  → a ranks first
        result = rrf([["a", "b"]], c=1)
        self.assertEqual(result[0], "a")
        # Exact scores: a gets 1/2, b gets 1/3
        self.assertAlmostEqual(1.0 / 2.0, 0.5, places=6)

    def test_rank_of(self):
        self.assertEqual(rank_of(["x", "y", "z"], {"z", "q"}), 3)
        self.assertIsNone(rank_of(["x"], {"q"}))

    def test_recall_and_mrr(self):
        ranks = [1, 3, None, 12]
        self.assertEqual(recall_at(ranks, 1), 0.25)
        self.assertEqual(recall_at(ranks, 5), 0.5)
        self.assertEqual(recall_at(ranks, 10), 0.5)
        self.assertAlmostEqual(mrr(ranks), (1 + 1 / 3 + 0 + 1 / 12) / 4)

    def test_recall_at_boundary_exactly_at_k(self):
        # Hit at rank k should be included in recall_at(k)
        ranks = [5, 10]
        self.assertEqual(recall_at(ranks, 5), 0.5)  # 1 hit at rank 5 out of 2

    def test_recall_at_boundary_just_after_k(self):
        # Hit at rank k+1 should not be included in recall_at(k)
        ranks = [6, 10]
        self.assertEqual(recall_at(ranks, 5), 0.0)  # no hits within rank 5

    def test_empty_inputs_score_zero(self):
        self.assertEqual((recall_at([], 5), mrr([])), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()

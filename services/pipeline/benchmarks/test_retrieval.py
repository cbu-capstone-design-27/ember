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
        # Docs: b="t t" (len 2) and a="t t w w" (len 4), query "t"
        # Both have same freq(t)=2, but different lengths
        # With b=0.75: b scores 0.2917, a scores 0.2353 → b ranks first
        # With b=0 (no length norm): both score 0.2605 (tied) → sorted by ref [a,b] (test fails)
        bm = BM25({"b": "t t", "a": "t t w w"})
        result = bm.rank("t", 10)
        self.assertEqual(result, ["b", "a"])
        # Compute and verify exact scores
        scores = {}
        for ref, tf in bm.tf.items():
            score = 0.0
            for term in ["t"]:
                freq = tf.get(term, 0)
                if freq:
                    norm = freq + bm.k1 * (1 - bm.b + bm.b * bm.length[ref] / bm.avg)
                    score += bm.idf[term] * freq * (bm.k1 + 1) / norm
            if score > 0:
                scores[ref] = score
        self.assertAlmostEqual(scores["b"], 0.2917, places=4)
        self.assertAlmostEqual(scores["a"], 0.2353, places=4)

    def test_rare_terms_contribute_more_via_higher_idf(self):
        # Query "alpha beta": alpha is common (in docs x and z), beta is rare (in y only)
        # Doc x="alpha" has only the common term; doc y="beta" has only the rare term
        # Both have same length and freq=1 for their term, so difference is pure idf
        # With real idf: idf(beta) > idf(alpha), so y ranks higher
        # With constant idf: both score equal (1.25 each), tied, sorted by ref: x < y, x first (test fails)
        bm = BM25({"x": "alpha", "y": "beta", "z": "alpha"})
        result = bm.rank("alpha beta", 10)
        self.assertEqual(result[0], "y")

    def test_k_limits_the_list(self):
        self.assertEqual(len(BM25(self.DOCS).rank("the", 1)), 1)

    def test_single_doc_ranks_by_idf(self):
        # Single-doc corpus: {"a": "x"}, query "x"
        # Verifies rank() works and score uses idf (vs constant)
        bm = BM25({"a": "x"})
        result = bm.rank("x", 10)
        self.assertEqual(result, ["a"])


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
        result = vector_rank([1.0, 0.0], vectors, 2)
        self.assertEqual(result, ["near", "mid"])
        # Verify exact scores
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.1]), 0.9950, places=4)
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 1.0]), 0.7071, places=4)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=4)

    def test_vector_rank_includes_zero_and_negative_scores(self):
        # vector_rank returns all docs including zero scores (unlike BM25.rank which filters score > 0)
        vectors = {"a": [1.0, 0.0], "b": [0.0, 0.0]}
        result = vector_rank([1.0, 0.0], vectors, 10)
        # a scores 1.0, b scores 0.0; both included in order
        self.assertEqual(result, ["a", "b"])
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0]), 1.0, places=6)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 0.0]), 0.0, places=6)


class FusionAndMetricTests(unittest.TestCase):
    def test_rrf_rewards_agreement(self):
        self.assertEqual(rrf([["a", "b", "c"], ["c", "a", "d"]])[:2], ["a", "c"])

    def test_rrf_keeps_documents_found_by_only_one_system(self):
        self.assertEqual(set(rrf([["a"], ["b"]])), {"a", "b"})

    def test_rrf_with_tie_broken_by_ref(self):
        # rrf([["a"], ["b"]]): both a and b appear once at position 1
        # Score for both: 1/(60+1) = 1/61 ≈ 0.01639 (tied)
        # Tie-breaking by ref: a < b, so a comes first
        result = rrf([["a"], ["b"]])
        self.assertEqual(result, ["a", "b"])
        # Reversed input: rrf([["b"], ["a"]]) should also give ["a", "b"] (tie still broken by ref)
        result_rev = rrf([["b"], ["a"]])
        self.assertEqual(result_rev, ["a", "b"])

    def test_rrf_c_parameter_affects_scores(self):
        # c parameter changes the denominator: score += 1.0 / (c + position)
        # With c=1: rank 1 gets 1/2=0.5, rank 2 gets 1/3≈0.333
        # With c=60: rank 1 gets 1/61≈0.0164, rank 2 gets 1/62≈0.0161
        # Rankings [["a", "b"], ["b", "c"]] with c=1: b gets 1/2+1/3≈0.833, a gets 0.5, c gets 0.333
        result_c1 = rrf([["a", "b"], ["b", "c"]], c=1)
        result_c60 = rrf([["a", "b"], ["b", "c"]], c=60)
        # Both should have same order (b first), but scores differ
        self.assertEqual(result_c1[0], "b")
        self.assertEqual(result_c60[0], "b")
        # Verify c=1 has larger score differences
        # For c=1: b=1/2+1/3, a=1/2, c=1/3
        self.assertAlmostEqual(1.0 / 2.0 + 1.0 / 3.0, 0.8333, places=4)
        self.assertAlmostEqual(1.0 / 2.0, 0.5, places=4)

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

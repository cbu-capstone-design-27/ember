import asyncio
import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from loader.memory_writer import MemoryWriter
from loader.records import EdgeRec, NodeRec
from ontology.config import EmbeddingIdentity

from .graph import Edge, Graph, Node, embed_graph, from_memory
from .mentions import link_mentions
from .photon import export_photon
from .suite import Question, evaluate, load_questions, metrics, validate_questions
from .systems import METHODS, Result, Retriever, adjacency, diffuse, pagerank, registry

A = "jira:WorkItem:PHO-1"
B = "slack:Message:C1:1000000000.000001"
C = "jira:WorkItem:PHO-2"
HUB = "jira:Person:p"


def graph():
    return Graph("photon", (Node(A, "jira", "WorkItem", "login timeout", vector=(1., 0., 0.)),
                            Node(B, "slack", "Message", "the patch shipped", vector=(0., 1., 0.)),
                            Node(C, "jira", "WorkItem", "billing report", vector=(0., 0., 1.)),
                            Node(HUB, "jira", "Person", "an author")),
                 (Edge(A, B, "REFERENCES"), Edge(A, HUB, "AUTHORED_BY"), Edge(C, HUB, "AUTHORED_BY")), "test-model:v1:3")


def question(**changes):
    return replace(Question("q1", "login", (A,), "single-source", family="topic-1", reviewed=True), **changes)


class ExportTests(unittest.TestCase):
    def test_updated_text_cannot_export_a_stale_embedding(self):
        import hashlib
        writer = MemoryWriter()
        writer.identity = EmbeddingIdentity("test", "v1", 2, "{query}")
        node = NodeRec("WorkItem", "photon_jira", "PHO-1", {"external_id": "PHO-1", "embed_text": "old",
                       "embedding": [1., 0.], "embedding_text_hash": hashlib.sha256(b"old").hexdigest()}, "2026-01-01")
        writer.write_nodes([node])
        self.assertEqual(from_memory(writer, "photon").documents[0].vector, (1., 0.))
        writer.write_nodes([NodeRec("WorkItem", "photon_jira", "PHO-1", {"embed_text": "new"}, "2026-02-01")])
        self.assertEqual(from_memory(writer, "photon").documents[0].vector, ())

    def test_current_tenant_only_and_tombstones_do_not_leak_into_export(self):
        writer = MemoryWriter()
        def node(group, key, text, stamp):
            return NodeRec("WorkItem", group, key, {"external_id": key, "embed_text": text}, stamp)
        writer.write_nodes([node("photon_jira", "PHO-1", "old", "2026-01-01"),
                            node("photon_jira", "PHO-1", "current", "2026-02-01"),
                            node("other_jira", "PHO-1", "secret", "2026-03-01")])
        writer.write_nodes([node("photon_jira", "PHO-1", "late old", "2026-01-02")])
        writer.write_edges([EdgeRec("RELATES_TO", "photon_jira", "WorkItem", "PHO-1", "WorkItem", "missing")], placeholders=True)
        exported = from_memory(writer, "photon")
        self.assertEqual([(n.ref, n.text) for n in exported.nodes], [(A, "current")])
        self.assertFalse(exported.edges)
        writer.write_nodes([NodeRec("WorkItem", "photon_jira", "PHO-1", {"deleted": True}, "2026-04-01"),
                            node("photon_jira", "PHO-2", "survivor", "2026-04-02")])
        self.assertEqual([n.ref for n in from_memory(writer, "photon").nodes], [C])

    def test_roundtrip_and_fingerprint_include_vectors_edges_and_identity(self):
        original = graph()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "graph.json"
            original.save(path)
            self.assertEqual(Graph.load(path), original)
        self.assertEqual(original.fingerprint, replace(original, nodes=tuple(reversed(original.nodes))).fingerprint)
        self.assertNotEqual(original.fingerprint, original.without_pairs([(A, B)]).fingerprint)
        self.assertNotEqual(original.fingerprint, replace(original, identity="another").fingerprint)

    def test_invalid_endpoints_vectors_or_identity_fail_closed(self):
        for mutate in (lambda g: replace(g, edges=(Edge(A, "unknown", "REFERENCES"),)),
                       lambda g: replace(g, nodes=(*g.nodes, g.nodes[0])),
                       lambda g: replace(g, identity="none"),
                       lambda g: replace(g, nodes=(replace(g.nodes[0], vector=(math.nan,)),)),
                       lambda g: replace(g, nodes=(replace(g.nodes[0], vector=(0., 0.)),))):
            with self.assertRaises(ValueError):
                mutate(graph())

    def test_embedding_cache_changes_only_for_changed_text_or_model(self):
        raw = Graph("photon", tuple(replace(n, vector=()) for n in graph().nodes), ())
        cache, calls = {}, []
        def embed(texts, **kwargs):
            self.assertFalse(kwargs["is_query"])
            calls.append(texts)
            return [[len(text), 1.] for text in texts]
        first = embed_graph(raw, embed, "model:v1", cache)
        self.assertEqual(len(calls[0]), 3)
        self.assertEqual(embed_graph(raw, embed, "model:v1", cache), first)
        self.assertEqual(len(calls), 1)
        changed = replace(raw, nodes=(replace(raw.nodes[0], text="new content"), *raw.nodes[1:]))
        embed_graph(changed, embed, "model:v1", cache)
        self.assertEqual(calls[-1], ["new content"])
        embed_graph(changed, embed, "model:v2", cache)
        self.assertEqual(len(calls[-1]), 3)

    def test_invalid_embedding_response_cannot_poison_cache(self):
        raw = Graph("photon", (Node(A, "jira", "WorkItem", "login"),), ())
        for response in ([], [[math.inf]], [[0.]], [[]]):
            cache = {}
            with self.assertRaises(ValueError):
                embed_graph(raw, lambda *a, **k: response, "model", cache)
            self.assertEqual(cache, {})

    def test_photon_replays_all_source_records_and_rebuilds_a_past_snapshot(self):
        full = asyncio.run(export_photon())
        self.assertEqual(len(full.documents), 778)
        before = datetime(2026, 9, 15, 15, tzinfo=timezone.utc)
        past = asyncio.run(export_photon(before))
        self.assertLess(len(past.documents), len(full.documents))
        self.assertTrue(all(n.occurred_at <= "2026-09-15T15:00:00.000000Z" for n in past.nodes))
        self.assertNotIn("slack:Message:C08PHOTONDV:1789488300.000100", {n.ref for n in past.nodes})
        with self.assertRaises(ValueError):
            asyncio.run(export_photon(datetime(2026, 9, 15)))


class MentionTests(unittest.TestCase):
    def resolve(self, text, **kwargs):
        return link_mentions(Graph("photon", (Node(A, "jira", "WorkItem", "ticket"),
                                              Node(B, "slack", "Message", text)), ()), **kwargs)

    def test_idempotent_mentions_and_allowlisted_jira_urls(self):
        linked, evidence = self.resolve("PHO-1 and <https://trusted.atlassian.net/browse/PHO-1|ticket>", jira_hosts=["trusted.atlassian.net"])
        self.assertEqual(linked.edges, (Edge(B, A, "REFERENCES"),))
        self.assertTrue(all(e.reason == "resolved" for e in evidence))
        self.assertEqual(link_mentions(linked, ["trusted.atlassian.net"])[0], linked)

    def test_foreign_urls_missing_targets_and_bare_github_are_not_guessed(self):
        linked, evidence = self.resolve("https://evil.example/browse/PHO-1 PHO-99 #12")
        self.assertFalse(linked.edges)
        self.assertEqual({e.reason for e in evidence}, {"target_not_loaded", "missing_repository_context"})

    def test_malformed_url_is_evidence_not_a_failed_scan(self):
        linked, evidence = self.resolve("https://[invalid PHO-1")
        self.assertEqual(linked.edges, (Edge(B, A, "REFERENCES"),))
        self.assertIn("invalid_url", {e.reason for e in evidence})

    def test_ambiguous_issue_and_pr_number_cannot_create_wrong_edge(self):
        github = (Node("github:WorkItem:org/repo#1", "github", "WorkItem", "issue"),
                  Node("github:Change:org/repo#1", "github", "Change", "pr"))
        source = Node(B, "slack", "Message", "https://github.com/org/repo/pull/1")
        linked, evidence = link_mentions(Graph("photon", (*github, source), ()))
        self.assertFalse(linked.edges)
        self.assertEqual(evidence[0].reason, "ambiguous")

    def test_github_bare_number_requires_matching_repository(self):
        nodes = (Node("github:Change:org/repo#2", "github", "Change", "fixes #1"),
                 Node("github:WorkItem:org/repo#1", "github", "WorkItem", "issue"),
                 Node("github:WorkItem:other/repo#1", "github", "WorkItem", "other issue"))
        linked, _ = link_mentions(Graph("photon", nodes, ()))
        self.assertEqual(linked.edges, (Edge(nodes[0].ref, nodes[1].ref, "REFERENCES"),))

    def test_slack_permalink_keeps_microsecond_precision_and_host_scope(self):
        linked, _ = self.resolve("PHO-1")
        nodes = (replace(linked.nodes[0], text="https://team.slack.com/archives/C1/p1000000000000001"), linked.nodes[1])
        untrusted, _ = link_mentions(Graph("photon", nodes, ()))
        trusted, _ = link_mentions(Graph("photon", nodes, ()), slack_hosts=["team.slack.com"])
        self.assertEqual(untrusted.edges, (Edge(B, A, "REFERENCES"),))
        self.assertEqual(trusted.edges, (Edge(A, B, "REFERENCES"), Edge(B, A, "REFERENCES")))


class AlgorithmTests(unittest.TestCase):
    def test_hubs_cannot_connect_unrelated_content(self):
        self.assertEqual(adjacency(graph()), {A: (B,), C: (), B: (A,)})

    def test_ppr_conserves_mass_and_dangling_mass_returns_to_seed(self):
        scores = pagerank(adjacency(graph()), {A: 1})
        self.assertAlmostEqual(sum(scores.values()), 1.)
        self.assertGreater(scores[B], scores[C])
        self.assertEqual(pagerank({A: ()}, {A: 1}), {A: 1.})
        with self.assertRaises(ValueError):
            pagerank({A: ()}, {A: -1})

    def test_diffusion_uses_linked_context_and_restart_retains_own_identity(self):
        features = {n.ref: n.vector for n in graph().documents}
        smoothed = diffuse(adjacency(graph()), features)
        self.assertGreater(smoothed[B][0], 0.)
        self.assertEqual(smoothed[C], [0., 0., 1.])
        self.assertEqual(diffuse(adjacency(graph()), features, alpha=1)[B], [0., 1., 0.])

    def test_every_method_implements_query_and_related_without_returning_anchor(self):
        for method in METHODS:
            with self.subTest(method=method):
                system = Retriever(graph(), method, {"login": (1., 0., 0.)})
                self.assertEqual(len(set(system.retrieve("login", "photon").refs)), len(system.retrieve("login", "photon").refs))
                related = system.related(A, "photon").refs
                self.assertNotIn(A, related)
                self.assertLessEqual(len(related), 2)
                with self.assertRaises(ValueError):
                    system.retrieve("login", "other")
                with self.assertRaises(ValueError):
                    system.related(A, "photon", 0)

    def test_structural_projection_is_reproducible_and_semantics_need_vectors(self):
        self.assertEqual(Retriever(graph(), "structural").structural, Retriever(graph(), "structural").structural)
        empty = Graph("photon", (Node(A, "jira", "WorkItem", "login"),), ())
        self.assertEqual(Retriever(empty, "bm25").retrieve("login", "photon").refs, (A,))
        with self.assertRaises(ValueError):
            Retriever(empty, "hybrid")


class EvaluationTests(unittest.TestCase):
    def test_recall_is_fraction_of_all_answers_and_ndcg_rewards_early_hits(self):
        result = metrics([A, C], [A, B])
        self.assertEqual(result["recall@5"], .5)
        self.assertEqual(result["mrr@10"], 1.)
        self.assertAlmostEqual(result["ndcg@10"], 1 / (1 + 1 / math.log2(3)))
        self.assertLess(metrics([C, A], [A])["ndcg@10"], metrics([A, C], [A])["ndcg@10"])
        with self.assertRaises(ValueError):
            metrics([A, A], [A])

    def test_split_family_and_duplicate_question_guard(self):
        with self.assertRaises(ValueError):
            question(reviewed="false")
        for other in (question(id="q2", text="  LOGIN "),
                      question(id="q2", text="a new query", split="test")):
            with self.assertRaises(ValueError):
                validate_questions([question(), other])

    def test_human_review_and_test_guards_and_model_identity(self):
        with self.assertRaises(ValueError):
            evaluate(graph(), [question(reviewed=False)], registry(["bm25"]))
        with self.assertRaises(ValueError):
            evaluate(graph(), [question(split="test")], registry(["bm25"]), split="test")
        with self.assertRaises(ValueError):
            evaluate(graph(), [question()], registry(["vector"]), query_vectors={"login": [1., 0., 0.]}, query_identity="wrong")

    def test_holdout_masks_all_directions_and_types_before_factory_and_derived_vectors(self):
        original = replace(graph(), edges=(*graph().edges, Edge(B, A, "RELATED_TO"), Edge(A, B, "OTHER")))
        related = question(category="related", answers=(B,), anchor=A, hidden_pairs=((A, B),))
        def factory(masked, vectors):
            self.assertFalse(any(e.src == B or e.dst == B for e in masked.edges))
            self.assertEqual(diffuse(adjacency(masked), {n.ref: n.vector for n in masked.documents})[B], [0., 1., 0.])
            class System:
                def related(self, *args):
                    return Result((B,))
            return System()
        report = evaluate(original, [related], {"spy": factory})
        self.assertEqual(report["hidden_pairs"], 1)
        self.assertEqual(report["systems"]["spy"]["scores"]["related"]["recall@10"], 1.)
        self.assertNotEqual(report["masked_graph_fingerprint"], original.fingerprint)

    def test_aggregate_report_contains_no_question_text_or_record_refs(self):
        report = evaluate(graph(), [question()], registry(["bm25"]))
        encoded = json.dumps(report)
        self.assertNotIn(A, encoded)
        self.assertNotIn('"login"', encoded)
        self.assertEqual(report["systems"]["bm25"]["scores"]["all"]["n"], 1)

    def test_invalid_plugin_candidates_cannot_silently_inflate_scores(self):
        class System:
            def retrieve(self, *args):
                return Result(("foreign:record",))
        with self.assertRaises(ValueError):
            evaluate(graph(), [question()], {"bad": lambda *a: System()})

    def test_draft_bank_is_grounded_but_not_a_sealed_test(self):
        path = Path(__file__).resolve().parents[1] / "questions" / "photon-v2-draft.json"
        questions = load_questions(path)
        self.assertEqual(len(questions), 60)
        self.assertEqual({q.category for q in questions}, {"single-source", "cross-source", "related"})
        self.assertTrue(all(q.split == "validation" and not q.reviewed for q in questions))
        exported = asyncio.run(export_photon())
        evaluate(exported, questions, registry(["bm25"]), allow_unreviewed=True)


if __name__ == "__main__":
    unittest.main()

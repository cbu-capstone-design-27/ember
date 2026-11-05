"""Offline comparison. Defaults to BM25; simulated embeddings always require an explicit diagnostic flag."""

import argparse
import asyncio
import json
import time
from pathlib import Path

from ..testing import bag_of_words_embed
from .graph import Graph, embed_graph
from .mentions import link_mentions
from .photon import export_photon
from .suite import evaluate, load_questions
from .systems import registry

QUESTIONS = Path(__file__).resolve().parents[1] / "questions" / "photon-v2-draft.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, help="one tenant's JSON export; defaults to committed photon fixtures")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--output", type=Path, required=True, help="aggregate result JSON (keep real-data outputs local)")
    parser.add_argument("--save-graph", type=Path, help="optional local content/vector export")
    parser.add_argument("--queries", type=Path, help="raw query vectors: {identity: ..., vectors: {text: [...]}}")
    parser.add_argument("--methods", nargs="+", default=["bm25"])
    parser.add_argument("--link-mentions", action="store_true")
    parser.add_argument("--jira-host", action="append", default=[])
    parser.add_argument("--slack-host", action="append", default=[])
    parser.add_argument("--diagnostic-bag", action="store_true", help="fake embeddings, pipeline diagnostics only")
    parser.add_argument("--allow-draft", action="store_true", help="unreviewed validation diagnostics only")
    parser.add_argument("--split", choices=["validation", "test"], default="validation")
    parser.add_argument("--unlock-test", action="store_true")
    args = parser.parse_args(argv)
    if args.diagnostic_bag and args.queries:
        parser.error("choose imported real query vectors or diagnostic embeddings")
    if args.diagnostic_bag and args.split == "test":
        parser.error("diagnostic embeddings cannot produce test results")
    started = time.perf_counter()
    graph = Graph.load(args.graph) if args.graph else asyncio.run(export_photon())
    questions = load_questions(args.questions)
    evidence = ()
    if args.link_mentions:
        graph, evidence = link_mentions(graph, args.jira_host, args.slack_host)
    vectors, identity = {}, None
    if args.diagnostic_bag:
        identity = "DIAGNOSTIC:bag-of-words:md5:64:v1"
        graph = embed_graph(graph, bag_of_words_embed, identity)
        texts = list(dict.fromkeys(q.text for q in questions if q.split == args.split and not q.anchor))
        vectors = dict(zip(texts, bag_of_words_embed(texts), strict=True))
    elif args.queries:
        raw = json.loads(args.queries.read_text(encoding="utf-8"))
        identity, vectors = raw["identity"], raw["vectors"]
    prep_ms = (time.perf_counter() - started) * 1000
    result = evaluate(graph, questions, registry(args.methods), split=args.split, query_vectors=vectors,
                      query_identity=identity, allow_unreviewed=args.allow_draft, unlock_test=args.unlock_test)
    result.update(diagnostic=args.diagnostic_bag, preparation_ms=prep_ms,
                  embedding_latency_ms=None, embedding_tokens=None,
                  latency_scope="retrieval only; graph build and preparation reported separately; imported embedding cost unknown",
                  mention_counts={reason: sum(e.reason == reason for e in evidence)
                                  for reason in sorted({e.reason for e in evidence})})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    if args.save_graph:
        args.save_graph.parent.mkdir(parents=True, exist_ok=True)
        graph.save(args.save_graph)
    print(json.dumps({"systems": len(result["systems"]), "human_reviewed": result["human_reviewed"],
                      "diagnostic": result["diagnostic"], "output": str(args.output)}))


if __name__ == "__main__":
    main()

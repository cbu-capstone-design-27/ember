"""Retrieval suite: given a question, does each search method find the node a person says answers it?"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .results import BenchmarkError
from .retrieval import BM25, mrr, rank_of, recall_at, rrf, vector_rank

SYSTEMS = ("bm25", "vector", "hybrid")


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    answers: tuple[str, ...]


@dataclass(frozen=True)
class Doc:
    ref: str
    text: str
    vector: list[float]


def load_questions(path: Path) -> tuple[dict, list[Question]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BenchmarkError(f"{path}: the top level must be an object with a questions list")
    items = data.get("questions")
    if not isinstance(items, list) or not items:
        raise BenchmarkError(f"{path}: needs a non-empty questions list")
    questions, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise BenchmarkError(f"{path}: each question must be an object with id, question and answers")
        qid, text, answers = item.get("id"), item.get("question"), item.get("answers")
        if not isinstance(qid, str) or not qid or qid in seen:
            raise BenchmarkError(f"{path}: question id {qid!r} is missing, not a string or repeated")
        if not isinstance(text, str) or not text.strip():
            raise BenchmarkError(f"{path}: question {qid} needs text (a string)")
        if not (isinstance(answers, list) and answers
                and all(isinstance(a, str) and a.strip() for a in answers)):
            raise BenchmarkError(f"{path}: question {qid} needs at least one answer, as a list of non-empty strings")
        seen.add(qid)
        questions.append(Question(qid, text.strip(), tuple(answers)))
    return {k: v for k, v in data.items() if k != "questions"}, questions


def docs_from(candidates: list[dict]) -> list[Doc]:
    docs = []
    for c in candidates:
        if c["vector"] is None:
            raise BenchmarkError(f"{c['key']} has no vector; embed the graph first (run with an embedding model)")
        docs.append(Doc(f"{c['group_id'].rsplit('_', 1)[1]}:{c['key']}", c["text"], c["vector"]))
    return docs


def run_retrieval(questions: list[Question], docs: list[Doc], embed_queries: Callable,
                  ks: tuple[int, ...] = (1, 3, 5, 10), depth: int = 50) -> dict:
    known = {d.ref for d in docs}
    unknown = sorted({a for q in questions for a in q.answers} - known)
    if unknown:
        raise BenchmarkError("answers not found among the embedded nodes: " + ", ".join(unknown))

    bm25 = BM25({d.ref: d.text for d in docs})
    vectors = {d.ref: d.vector for d in docs}
    query_vectors = embed_queries([q.text for q in questions], is_query=True)

    ranks: dict[str, list[int | None]] = {s: [] for s in SYSTEMS}
    details = []
    for question, query_vector in zip(questions, query_vectors):
        lexical = bm25.rank(question.text, depth)
        semantic = vector_rank(query_vector, vectors, depth)
        lists = {"bm25": lexical, "vector": semantic, "hybrid": rrf([lexical, semantic])[:depth]}
        row = {system: rank_of(ranked, question.answers) for system, ranked in lists.items()}
        for system in SYSTEMS:
            ranks[system].append(row[system])
        details.append({"id": question.id, "answers": list(question.answers), "ranks": row})

    scores = {system: {**{f"recall@{k}": round(recall_at(ranks[system], k), 4) for k in ks},
                       "mrr": round(mrr(ranks[system]), 4)} for system in SYSTEMS}
    return {"scores": scores, "details": details}

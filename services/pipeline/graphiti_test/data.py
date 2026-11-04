"""The photon records, what each one looks like as a Graphiti episode, and the questions asked of the graph.

Rows come from apps/mcp/seed_photon.py's rows(), the same ones the MCP tests and EMBER-86 use. Nothing here
needs Postgres or Graphiti.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from loader.mappers.jira import adf_text

PIPELINE = Path(__file__).resolve().parents[1]
REPO = PIPELINE.parents[1]
FIXTURES = PIPELINE / "fixtures" / "photon"
SEED = REPO / "apps" / "mcp" / "seed_photon.py"


@dataclass(frozen=True)
class Row:
    source: str
    kind: str
    object_id: str
    version: str
    occurred_at: datetime
    record: dict

    @property
    def name(self) -> str:
        """The episode's name in the graph, and its key in the ledger. Stable across runs and backends."""
        return f"{self.source}/{self.kind}/{self.object_id}@{self.version}"

    @property
    def order(self) -> tuple:
        """The source's ordering value (EMBER-63), then a stable tie-break."""
        return (self.occurred_at, self.source, self.kind, self.object_id, self.version)


def record_of(episode_name: str) -> str:
    """'jira/issue/10087@<version>' -> '10087': the object id that questions name as their expected answer."""
    return episode_name.rsplit("@", 1)[0].split("/", 2)[2]


@lru_cache(maxsize=1)
def load_rows() -> tuple[Row, ...]:
    spec = importlib.util.spec_from_file_location("seed_photon", SEED)
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    return tuple(sorted((Row(*row) for row in seed.rows()), key=lambda r: r.order))


# --- episodes ---

@lru_cache(maxsize=1)
def slack_names() -> tuple[dict[str, str], dict[str, str]]:
    users, channels = {}, {}
    for line in (FIXTURES / "slack-intake.jsonl").read_text(encoding="utf-8").splitlines():
        body = json.loads(line)["body"]
        if "real_name" in body:
            users[body["id"]] = body["real_name"]
        elif "is_channel" in body or "num_members" in body:
            channels[body["id"]] = body["name"]
    return users, channels


def _person(value: dict | None) -> str | None:
    return (value or {}).get("displayName")


def _linked_issues(fields: dict) -> list[dict]:
    links = []
    for link in fields.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue") or {}
        phrase = link["type"]["outward" if "outwardIssue" in link else "inward"]
        links.append({"relation": phrase, "issue": other.get("key")})
    return links


def render(row: Row) -> dict[str, Any]:
    """add_episode's name, body, source type and source description for a row.

    The ids are in the text because the ontology's SourceRef types (WorkItem, Message, ...) require source, kind
    and external_id as attributes, and attribute extraction can only read them from the episode.
    `source` is the name of a graphiti_core EpisodeType, resolved where graphiti_core is imported.
    """
    record = row.record
    if (row.source, row.kind) == ("jira", "issue"):
        issue = record["issue"]
        fields = issue["fields"]
        body = {
            "source": "jira", "kind": "issue", "external_id": issue["key"],
            "summary": fields.get("summary"),
            "issue_type": (fields.get("issuetype") or {}).get("name"),
            "status": (fields.get("status") or {}).get("name"),
            "priority": (fields.get("priority") or {}).get("name"),
            "project": (fields.get("project") or {}).get("key"),
            "parent": (fields.get("parent") or {}).get("key"),
            "reporter": _person(fields.get("reporter")),
            "assignee": _person(fields.get("assignee")),
            "labels": fields.get("labels") or [],
            "links": _linked_issues(fields),
            "description": adf_text(fields.get("description")),
        }
        text = json.dumps({k: v for k, v in body.items() if v not in (None, [])}, ensure_ascii=False)
        return {"name": row.name, "episode_body": text, "source": "json", "source_description": "Jira issue"}
    if (row.source, row.kind) == ("jira", "comment"):
        comment, key = record["comment"], record["issue"]["key"]
        text = (f"{_person(comment.get('author'))}: (comment {comment['id']} on Jira issue {key}) "
                f"{adf_text(comment.get('body'))}")
        return {"name": row.name, "episode_body": text, "source": "message", "source_description": "Jira comment"}
    if (row.source, row.kind) == ("slack", "message"):
        users, channels = slack_names()
        channel = channels.get(record["channel"], record["channel"])
        author = users.get(record.get("user")) or record.get("username") or record.get("user") or "unknown"
        where = f"message {row.object_id} in #{channel}"
        if record.get("thread_ts") and record["thread_ts"] != record["ts"]:
            where += f", replying in thread {record['thread_ts']}"
        text = f"{author}: ({where}) {record['text']}"
        return {"name": row.name, "episode_body": text, "source": "message",
                "source_description": f"Slack message in #{channel}"}
    raise ValueError(f"no episode shape for {row.source}/{row.kind}")


# --- questions ---

# EMBER-78's acceptance questions, with their expected records, as in apps/mcp/test_mcp.py.
EMBER_78 = {
    "Why can't I use dots in branch names?": ["C08PHOTONDV/1789392360.000100"],
    "Caps Lock turns on when I use it as a Hyper key": ["10087"],  # PHO-87
    "Why do launched programs stay hidden behind other windows?": ["C08PHOTONBG/1789971000.000100"],
    "Running a development build quits the release app": ["10105"],  # PHO-105, Photon-Dev
    "Pressing the arrow keys in clipboard history doesn't move between items": ["10036"],  # PHO-36
}


def _issue_id(rows, key: str) -> str:
    return next(r.object_id for r in rows if r.kind == "issue" and r.record["issue"]["key"] == key)


def _ember_86(rows) -> list[dict]:
    """EMBER-86's query shapes, as questions, with the records that hold each answer.

    subgraph-isolation is structural, not a search: graph_reader answers it. issue-state's "how many issues in
    each status" is an aggregate no ranked search can answer, so only PHO-19's own record is expected.
    """
    document = json.loads((FIXTURES / "expected-graph.json").read_text(encoding="utf-8"))
    queries = {q["id"]: q for q in document["queries"]}
    thread_channel, thread_ts = queries["thread-messages"]["params"]["thread"].split(":")
    expected = {
        "comment-authors": [r.object_id for r in rows if r.kind == "comment" and r.record["issue"]["key"] == "PHO-48"],
        "thread-messages": [r.object_id for r in rows if r.source == "slack" and r.record["channel"] == thread_channel
                            and thread_ts in (r.record["ts"], r.record.get("thread_ts"))],
        "issues-linked-to": [_issue_id(rows, key) for key in ("PHO-19", "PHO-10", "PHO-12")],
        "issue-state": [_issue_id(rows, "PHO-19")],
    }
    return [{"id": f"ember-86/{qid}", "query": queries[qid]["question"], "expected": ids}
            for qid, ids in expected.items()]


def questions(rows=None) -> list[dict]:
    rows = load_rows() if rows is None else rows
    numbered = enumerate(EMBER_78.items(), 1)
    return [{"id": f"ember-78/{i}", "query": q, "expected": ids} for i, (q, ids) in numbered] + _ember_86(rows)


# "What was true then": the release stack changes, in #photon-dev, from Rust + GPUI (2026-09-14 23:53 PT) back to
# Swift (2026-09-15 09:05 PT). Photon has no edited records (docs/pipeline/neo4j-load-check.md), so this decision
# is its one clear change of fact. `before` falls between the two messages, `after` follows the second.
TEMPORAL = {
    "id": "release-stack",
    "query": "What is Photon's release stack, Rust and GPUI or Swift?",
    "before": datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
    "after": datetime(2026, 9, 15, 17, 0, tzinfo=timezone.utc),
    "old_term": "gpui",
    "new_term": "swift",
    "messages": ("Going to try a Rust + GPUI rewrite", "Decided. GPUI did not pass."),
}


def temporal_rows(rows) -> list[Row]:
    return [r for r in rows if r.source == "slack" and any(m in r.record["text"] for m in TEMPORAL["messages"])]


def select_rows(rows, limit: int | None) -> list[Row]:
    """All rows, or a sample of `limit` that can still answer the questions.

    A sample of the earliest rows alone would hold no expected record and no change of fact, so those rows go
    first (earliest first if even they exceed the limit) and the earliest others fill the rest. Always in the
    source's order.
    """
    rows = sorted(rows, key=lambda r: r.order)
    if limit is None or limit >= len(rows):
        return rows
    wanted = {obj for q in questions(rows) for obj in q["expected"]} | {r.object_id for r in temporal_rows(rows)}
    anchors = [r for r in rows if r.object_id in wanted][:limit]
    chosen = {r.name for r in anchors}
    rest = [r for r in rows if r.name not in chosen][:limit - len(anchors)]
    return sorted(anchors + rest, key=lambda r: r.order)

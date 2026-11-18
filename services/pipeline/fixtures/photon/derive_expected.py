"""Derive what the graph should hold for the photon data, from the fixtures alone (EMBER-86).

    python3 services/pipeline/fixtures/photon/derive_expected.py          # rewrite expected-graph.json
    python3 services/pipeline/fixtures/photon/derive_expected.py --check  # fail if it is out of date

Stdlib only, and it imports nothing from the loader on purpose: the numbers come from counting the
intake envelopes (jira-intake.jsonl, slack-intake.jsonl) by the rules in docs/graph-schema.md and
docs/pipeline/load-check.md. So the loader's output is checked against something it did not produce.

Counting rules (what one Jira issue, Jira comment or Slack message becomes):

- Jira issue: a WorkItem, its project's Container (CONTAINS), the creator and the assignee as
  Identity + Person (RESOLVES_TO) with AUTHORED and ASSIGNED_TO edges, and a WorkItem RELATES_TO
  WorkItem edge per issue link. A link shows on both issues; both views are the same edge, from the
  outward issue to the inward one.
- Jira comment: a Message, a REFERENCES edge to its issue, and its author as Identity + Person
  with an AUTHORED edge.
- Slack message: a Message, its thread's Conversation (the thread_ts, or its own ts when it starts
  one), its channel's Container, CONTAINS (channel to thread) and PART_OF (message to thread) edges,
  REPLIES_TO from a reply to its thread's first message, and the sender as Identity + Person with an
  AUTHORED edge. Slack users and channels are not stored in source_records by seed_photon.py, so
  they appear only through their messages.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "expected-graph.json"
COMMENT_ISSUE = re.compile(r"/issue/(\d+)/comment/")


def envelopes(name: str) -> list[dict]:
    return [json.loads(line)["body"] for line in (HERE / name).read_text(encoding="utf-8").splitlines() if line]


def triple(src: str, rel: str, dst: str) -> str:
    return f"{src} {rel} {dst}"


# --- counts, from the envelopes ---

def jira_counts() -> dict:
    bodies = envelopes("jira-intake.jsonl")
    issues = {b["id"]: b for b in bodies if "fields" in b and "key" in b}
    comments = {}
    for b in bodies:
        if "author" in b and "body" in b and "/comment/" in b.get("self", ""):
            comments[b["id"]] = b
    keys = {i["key"] for i in issues.values()}

    projects, accounts = set(), set()
    edges: dict[str, set] = {name: set() for name in (
        "Container CONTAINS WorkItem", "Person AUTHORED WorkItem", "Person ASSIGNED_TO WorkItem",
        "WorkItem RELATES_TO WorkItem", "Person AUTHORED Message", "Message REFERENCES WorkItem")}
    for issue in issues.values():
        fields = issue["fields"]
        project = fields["project"]["key"]
        projects.add(project)
        edges["Container CONTAINS WorkItem"].add((project, issue["key"]))
        for field, rel in (("creator", "AUTHORED"), ("assignee", "ASSIGNED_TO")):
            person = fields.get(field)
            if person and person.get("accountId"):
                accounts.add(person["accountId"])
                edges[f"Person {rel} WorkItem"].add((person["accountId"], issue["key"]))
        for link in fields.get("issuelinks") or []:
            other = (link.get("outwardIssue") or link.get("inwardIssue"))["key"]
            assert other in keys, f"link target {other} is not an issue in the data (it would load as a placeholder)"
            pair = (issue["key"], other) if "outwardIssue" in link else (other, issue["key"])
            edges["WorkItem RELATES_TO WorkItem"].add(pair)
    for comment in comments.values():
        issue = issues[COMMENT_ISSUE.search(comment["self"]).group(1)]
        accounts.add(comment["author"]["accountId"])
        edges["Person AUTHORED Message"].add((comment["author"]["accountId"], comment["id"]))
        edges["Message REFERENCES WorkItem"].add((comment["id"], issue["key"]))

    nodes = {"WorkItem": len(issues), "Container": len(projects), "Message": len(comments),
             "Identity": len(accounts), "Person": len(accounts)}
    rels = {name: len(pairs) for name, pairs in edges.items()}
    rels["Identity RESOLVES_TO Person"] = len(accounts)
    return {"nodes": nodes, "relationships": rels}


def slack_messages() -> list[dict]:
    return [b for b in envelopes("slack-intake.jsonl") if "ts" in b and "text" in b and "channel" in b]


def slack_counts() -> dict:
    messages = {(m["channel"], m["ts"]): m for m in slack_messages()}
    threads = {(m["channel"], m.get("thread_ts") or m["ts"]) for m in messages.values()}
    replies = {k for k, m in messages.items() if m.get("thread_ts") and m["thread_ts"] != m["ts"]}
    for channel, ts in replies:
        parent = (channel, messages[(channel, ts)]["thread_ts"])
        assert parent in messages, f"reply {ts} in {channel} has no first message (it would load as a placeholder)"
    senders = {m["user"] for m in messages.values() if m.get("user")}
    nodes = {"Message": len(messages), "Conversation": len(threads),
             "Container": len({c for c, _ in messages}), "Identity": len(senders), "Person": len(senders)}
    rels = {
        "Container CONTAINS Conversation": len(threads),
        "Message PART_OF Conversation": len(messages),
        "Message REPLIES_TO Message": len(replies),
        "Person AUTHORED Message": len({k for k, m in messages.items() if m.get("user")}),
        "Identity RESOLVES_TO Person": len(senders),
    }
    return {"nodes": nodes, "relationships": rels}


# --- assembly ---

def build() -> dict:
    return {
        "tenant": "photon",
        "derived_by": "fixtures/photon/derive_expected.py, from the intake envelopes",
        "subgraphs": {"jira": jira_counts(), "slack": slack_counts()},
    }


def main(argv: list[str]) -> int:
    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print("expected-graph.json is out of date; run derive_expected.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Derive what the graph should hold for the photon data, from the fixtures alone (EMBER-86).

    python3 services/pipeline/fixtures/photon/derive_expected.py          # rewrite expected-graph.json
    python3 services/pipeline/fixtures/photon/derive_expected.py --check  # fail if it is out of date

Stdlib only, and it imports nothing from the loader on purpose: the numbers come from counting the
intake envelopes (jira-intake.jsonl, slack-intake.jsonl) by the rules in docs/graph-schema.md and
docs/pipeline/neo4j-load-check.md, and the answers to the sample queries come from the people-readable
transcripts (jira.md, slack.md). So the loader's output is checked against something it did not produce.

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
from collections import Counter
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


# --- sample query answers, from the transcripts ---

def jira_sections() -> dict[str, str]:
    text = (HERE / "jira.md").read_text(encoding="utf-8")
    parts = re.split(r"^### (PHO-\d+): .*$", text, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def jira_comment_authors(key: str) -> list[str]:
    """'> **Daniel Reyes** 2026-09-15 07:33 PT: ...' lines of the issue's section."""
    return sorted(set(re.findall(r"^> \*\*(.+?)\*\* \d{4}-", jira_sections()[key], flags=re.M)))


def jira_status(key: str) -> tuple[str, str]:
    """(issue type, status) from the 'Bug, Done (Done), priority High.' line."""
    match = re.search(r"^(\w+), (.+?)(?: \(.+?\))?, priority", jira_sections()[key], flags=re.M)
    return match.group(1), match.group(2)


def jira_created_by(key: str) -> tuple[str, str]:
    match = re.search(r"^Created .+? PT by (.+?), assigned to (.+?)\.", jira_sections()[key], flags=re.M)
    return match.group(1), match.group(2)


def jira_related(key: str) -> list[str]:
    """Issues linked 'relates to' either way: the issue's own lines plus other issues' lines naming it."""
    related = set(re.findall(r"^- relates to (PHO-\d+)$", jira_sections()[key], flags=re.M))
    for other, section in jira_sections().items():
        if re.search(rf"^- relates to {key}$", section, flags=re.M):
            related.add(other)
    return sorted(related)


def jira_status_totals() -> dict[str, int]:
    totals: Counter = Counter()
    for key in jira_sections():
        totals[jira_status(key)[1]] += 1
    return dict(sorted(totals.items()))


def slack_members() -> dict[str, str]:
    """display name -> Slack user id, from the '## Members' list."""
    return {name: uid for name, uid in re.findall(r"^- (.+?) \(`(U\w+)`", (HERE / "slack.md").read_text(encoding="utf-8"), flags=re.M)}


def slack_thread(channel_name: str, parent_start: str) -> tuple[list[tuple[str, str]], str]:
    """(author name, text) of a thread's messages in order, and the thread's id (channel id, thread_ts).

    The thread is found in slack.md by its channel and the start of its first message, which is the
    transcript's reading; the ids are found in the envelopes by the same text.
    """
    text = (HERE / "slack.md").read_text(encoding="utf-8")
    channel_section = re.search(rf"^## #{re.escape(channel_name)}\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S).group(1)
    lines = channel_section.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(r"- \*\*", line) and parent_start in line)
    thread = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("    - "):
            break
        thread.append(line)
    parsed = [re.match(r"\s*- \*\*(.+?)\*\* \d{4}-\d\d-\d\d \d\d:\d\d PT: (.*)$", line).groups() for line in thread]
    first = next(m for m in slack_messages() if m["text"].startswith(parent_start))
    return [(name, body) for name, body in parsed], f'{first["channel"]}:{first.get("thread_ts") or first["ts"]}'


# --- assembly ---

def build() -> dict:
    members = slack_members()
    by_id = {uid: name for name, uid in members.items()}
    posts, thread_key = slack_thread("photon-bugs", "0.3.0 is bad.")
    assert len(posts) >= 2
    comment_issue = "PHO-48"
    related_to = "PHO-19"
    status_issue = "PHO-19"
    type_, status = jira_status(status_issue)
    created_by, assignee = jira_created_by(comment_issue)
    queries = [
        {
            "id": "comment-authors",
            "question": f"Who commented on {comment_issue}?",
            "source": f"jira.md, section {comment_issue}: the '> **Name** date PT:' comment lines",
            "cypher": "MATCH (p:Person)-[:AUTHORED]->(:Message)-[:REFERENCES]->(w:WorkItem {group_id: $jira, external_id: $issue}) "
                      "WHERE p.group_id = $jira RETURN DISTINCT p.name AS person ORDER BY person",
            "params": {"issue": comment_issue},
            "expect": [{"person": name} for name in jira_comment_authors(comment_issue)],
        },
        {
            "id": "thread-messages",
            "question": f"Which messages are in the #photon-bugs thread that starts '{posts[0][1][:30]}...', and who sent them?",
            "source": "slack.md, section #photon-bugs: the parent line and its indented replies",
            "cypher": "MATCH (m:Message {group_id: $slack})-[:PART_OF]->(c:Conversation {group_id: $slack, external_id: $thread}) "
                      "MATCH (i:Identity {group_id: $slack})-[:RESOLVES_TO]->(:Person)-[:AUTHORED]->(m) "
                      "RETURN i.external_id AS sender, m.embed_text AS text ORDER BY m.created_at, m.external_id",
            "params": {"thread": thread_key},
            "expect": [{"sender": members[name], "text": body} for name, body in posts],
            "ordered": True,
        },
        {
            "id": "issues-linked-to",
            "question": f"Which issues are linked to {related_to} ('relates to')?",
            "source": f"jira.md: '- relates to' lines of {related_to} and of the issues naming it",
            "cypher": "MATCH (a:WorkItem {group_id: $jira})-[:RELATES_TO]-(b:WorkItem {group_id: $jira, external_id: $issue}) "
                      "RETURN DISTINCT a.external_id AS issue ORDER BY issue",
            "params": {"issue": related_to},
            "expect": [{"issue": key} for key in jira_related(related_to)],
        },
        {
            "id": "issue-state",
            "question": f"What are {status_issue}'s type and status, and how many issues are in each status?",
            "source": f"jira.md: the '{type_}, {status}' line of {status_issue}, and the status line of every issue",
            "cypher": "MATCH (w:WorkItem {group_id: $jira}) "
                      "RETURN w.status AS status, count(*) AS issues, "
                      "collect(CASE WHEN w.external_id = $issue THEN w.item_type END)[0] AS type_of_issue "
                      "ORDER BY status",
            "params": {"issue": status_issue},
            "expect": [{"status": s, "issues": n, "type_of_issue": type_ if s == status else None}
                       for s, n in jira_status_totals().items()],
        },
        {
            "id": "subgraph-isolation",
            "question": "Does anything cross from the Jira subgraph into the Slack one?",
            "source": "the data: Jira and Slack records are loaded into their own subgraphs, so no edge may join them "
                      "and each node's source must match its subgraph",
            "cypher": "MATCH (n) WHERE n.group_id IN [$jira, $slack] "
                      "WITH n.group_id AS grp, collect(DISTINCT n.source) AS sources, count(*) AS nodes "
                      "CALL { MATCH (a)-[r]->(b) WHERE a.group_id IN [$jira, $slack] AND a.group_id <> b.group_id "
                      "RETURN count(r) AS crossing } "
                      "RETURN grp, sources, nodes, crossing ORDER BY grp",
            "params": {},
            "expect": [{"grp": "{tenant}_jira", "sources": ["jira"], "nodes": None, "crossing": 0},
                       {"grp": "{tenant}_slack", "sources": ["slack"], "nodes": None, "crossing": 0}],
        },
    ]
    # the isolation answer's node counts are the counts above, summed (placeholders excluded: there are none)
    jira, slack = jira_counts(), slack_counts()
    queries[4]["expect"][0]["nodes"] = sum(jira["nodes"].values())
    queries[4]["expect"][1]["nodes"] = sum(slack["nodes"].values())
    _ = (created_by, assignee, by_id)
    return {
        "tenant": "photon",
        "derived_by": "fixtures/photon/derive_expected.py, from the intake envelopes and the readable transcripts",
        "subgraphs": {"jira": jira, "slack": slack},
        "queries": queries,
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

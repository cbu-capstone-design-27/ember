"""Deterministic mention evidence for an experiment export (EMBER-133).

No production writes or inferred fixes/causes. Ambiguous and missing targets are reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit

from .graph import Edge, Graph

URL = re.compile(r"https?://[^\s<>|]+")
JIRA = re.compile(r"(?<![\w/])([A-Z][A-Z0-9]+-\d+)\b")
GITHUB = re.compile(r"(?<![\w/])([\w.-]+/[\w.-]+)#([1-9]\d*)\b")
BARE = re.compile(r"(?<![\w/#])#([1-9]\d*)\b")


@dataclass(frozen=True)
class Evidence:
    origin: str
    target: str | None
    mention: str
    reason: str


def link_mentions(graph: Graph, jira_hosts=(), slack_hosts=()):
    """Resolve against a single tenant's current catalog. URL hosts require an explicit tenant allowlist.

    Bare Jira keys resolve only when that source/WorkItem key exists uniquely; GitHub #n additionally needs
    the origin's repository. Missing targets remain evidence for a future production placeholder adapter.
    """
    catalog = {}
    for node in graph.nodes:
        _, label, key = node.ref.split(":", 2)
        if label in {"WorkItem", "Change", "Message"}:
            catalog.setdefault((node.source, key), []).append(node.ref)
    evidence, edges = [], set(graph.edges)
    for node in graph.documents:
        text = node.text
        refs = []
        for match in URL.finditer(text):
            url = match.group().rstrip(".,);]}\"'")
            try:
                parsed = urlsplit(url)
            except ValueError:
                evidence.append(Evidence(node.ref, None, url, "invalid_url"))
                continue
            parts = unquote(parsed.path).strip("/").split("/")
            source, key = None, None
            if parsed.hostname == "github.com" and len(parts) >= 4 and parts[2] in {"issues", "pull"} and parts[3].isdigit():
                source, key = "github", f"{parts[0]}/{parts[1]}#{parts[3]}"
            elif parsed.hostname in jira_hosts and len(parts) == 2 and parts[0] == "browse" and JIRA.fullmatch(parts[1]):
                source, key = "jira", parts[1]
            elif parsed.hostname in slack_hosts and len(parts) == 3 and parts[0] == "archives" and re.fullmatch(r"p\d{16}", parts[2]):
                digits = parts[2][1:]
                source, key = "slack", f"{parts[1]}:{digits[:10]}.{digits[10:]}"
            if source:
                refs.append((source, key, url))
        # URL contents must not be reinterpreted as bare Jira keys, especially on an untrusted host.
        plain = URL.sub(" ", text)
        refs += [("jira", m.group(1), m.group()) for m in JIRA.finditer(plain)]
        refs += [("github", f"{m.group(1)}#{m.group(2)}", m.group()) for m in GITHUB.finditer(plain)]
        key = node.ref.split(":", 2)[2]
        context = re.match(r"^([\w.-]+/[\w.-]+)(?:#|@)", key) if node.source == "github" else None
        repository = context.group(1) if context else None
        for m in BARE.finditer(plain):
            if repository:
                refs.append(("github", f"{repository}#{m.group(1)}", m.group()))
            else:
                evidence.append(Evidence(node.ref, None, m.group(), "missing_repository_context"))
        for source, key, mention in sorted(set(refs)):
            targets = catalog.get((source, key), [])
            target = targets[0] if len(targets) == 1 else None
            reason = "resolved" if target else "ambiguous" if targets else "target_not_loaded"
            evidence.append(Evidence(node.ref, target, mention, reason))
            if target and target != node.ref:
                edges.add(Edge(node.ref, target, "REFERENCES"))
    return replace(graph, edges=tuple(sorted(edges))), tuple(evidence)

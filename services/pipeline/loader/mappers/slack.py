"""Slack objects (pull-script shapes and event callbacks) to graph records."""

from __future__ import annotations

from datetime import datetime, timezone

from ontology import subgraph_id

from ..records import EdgeRec
from .base import MapContext, Mapped, Reject, Skip, add_account, link_person, source_node

_IGNORED_SUBTYPES = {"message_changed", "message_deleted"}


def _classify(body: dict) -> tuple[str, dict]:
    event = body.get("event")
    if body.get("type") == "event_callback" and isinstance(event, dict):
        return ("message", event) if event.get("type") == "message" else ("event_other", event)
    if body.get("type") == "message" and "ts" in body:
        return "message", body
    if "is_channel" in body or "is_group" in body or ("is_private" in body and "name" in body):
        return "channel", body
    if "profile" in body or "real_name" in body or "is_bot" in body:
        return "user", body
    if "domain" in body and str(body.get("id", "")).startswith("T"):
        return "team", body
    return "other", body


def _iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def map_slack(ctx: MapContext, body: dict) -> Mapped:
    kind, obj = _classify(body)
    gid = subgraph_id(ctx.tenant, "slack")
    out = Mapped()

    if kind == "channel":
        out.nodes.append(source_node("Container", gid, "slack", "channel", obj["id"], name=obj.get("name")))
        return out

    if kind == "user":
        profile = obj.get("profile") or {}
        add_account(out, gid, "slack", obj["id"], handle=obj.get("name"),
                    name=obj.get("real_name") or profile.get("real_name"), email=profile.get("email"))
        return out

    if kind != "message":
        raise Skip(kind)
    if obj.get("subtype") in _IGNORED_SUBTYPES:
        raise Skip(obj["subtype"])
    channel, ts = obj.get("channel"), obj.get("ts")
    if not channel or not ts:
        raise Reject("message has no channel or ts")

    thread_ts = obj.get("thread_ts") or ts
    message_key = f"{channel}:{ts}"
    conversation_key = f"{channel}:{thread_ts}"
    text = obj.get("text") or ""

    container = source_node("Container", gid, "slack", "channel", channel)
    conversation = source_node("Conversation", gid, "slack", "thread", conversation_key)
    message = source_node("Message", gid, "slack", "message", message_key, name=text[:80] or None,
                          embed_text=text, created_at=_iso(ts))
    out.nodes += [container, conversation, message]
    out.edges += [
        EdgeRec("CONTAINS", gid, "Container", container.key, "Conversation", conversation_key),
        EdgeRec("PART_OF", gid, "Message", message_key, "Conversation", conversation_key),
    ]
    if thread_ts != ts:
        out.edges.append(EdgeRec("REPLIES_TO", gid, "Message", message_key, "Message", conversation_key))
    if obj.get("user"):
        link_person(out, gid, "slack", obj["user"], message, "AUTHORED")
    return out

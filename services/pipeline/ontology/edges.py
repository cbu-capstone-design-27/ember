"""Relationship types and the allowed (source, target) pairs for each (EMBER-34).

Edge names are SCREAMING_SNAKE. EDGE_TYPE_MAP has the shape Graphiti's
add_episode(edge_type_map=...) expects: {(source_label, target_label): [edge names]}.
"""

from pydantic import BaseModel, Field


class Authored(BaseModel):
    """A person wrote or created the target."""


class AssignedTo(BaseModel):
    """A person is assigned to the work item or change."""


class Reviewed(BaseModel):
    """A person reviewed the change."""

    verdict: str | None = Field(default=None, description="approved, changes_requested, commented.")


class MemberOf(BaseModel):
    """A person belongs to a repository, project, channel or team."""


class Owns(BaseModel):
    """A person is responsible for a module."""


class ResolvesTo(BaseModel):
    """An account identity belongs to this person."""


class Contains(BaseModel):
    """A container holds the target."""


class PartOf(BaseModel):
    """A message belongs to a conversation."""


class RepliesTo(BaseModel):
    """A message replies to another message."""


class References(BaseModel):
    """The source mentions or links to the target."""


class Resolves(BaseModel):
    """A change closes or fixes a work item."""


class RelatesTo(BaseModel):
    """Two work items are linked."""

    link_type: str | None = Field(default=None, description="blocks, duplicates, relates, ...")


class Touches(BaseModel):
    """A change modifies a module."""


class DecidedIn(BaseModel):
    """A decision was made in the target (a message, conversation, change or work item)."""


class Supersedes(BaseModel):
    """A newer decision replaces an older one."""


class Affects(BaseModel):
    """A decision constrains a module, container or work item."""


# name -> (model, [(source_label, target_label), ...])
_SPECS: dict[str, tuple[type[BaseModel], list[tuple[str, str]]]] = {
    "AUTHORED": (Authored, [("Person", t) for t in ("WorkItem", "Change", "Message", "Decision")]),
    "ASSIGNED_TO": (AssignedTo, [("Person", "WorkItem"), ("Person", "Change")]),
    "REVIEWED": (Reviewed, [("Person", "Change")]),
    "MEMBER_OF": (MemberOf, [("Person", "Container")]),
    "OWNS": (Owns, [("Person", "Module")]),
    "RESOLVES_TO": (ResolvesTo, [("Identity", "Person")]),
    "CONTAINS": (Contains, [("Container", t) for t in ("WorkItem", "Change", "Conversation", "Module")]),
    "PART_OF": (PartOf, [("Message", "Conversation")]),
    "REPLIES_TO": (RepliesTo, [("Message", "Message")]),
    "REFERENCES": (
        References,
        [
            (s, t)
            for s in ("Message", "WorkItem", "Change", "Decision")
            for t in ("WorkItem", "Change", "Person", "Module", "Decision", "Container")
        ],
    ),
    "RESOLVES": (Resolves, [("Change", "WorkItem")]),
    "RELATES_TO": (RelatesTo, [("WorkItem", "WorkItem")]),
    "TOUCHES": (Touches, [("Change", "Module")]),
    "DECIDED_IN": (DecidedIn, [("Decision", t) for t in ("Message", "Conversation", "Change", "WorkItem")]),
    "SUPERSEDES": (Supersedes, [("Decision", "Decision")]),
    "AFFECTS": (Affects, [("Decision", t) for t in ("Module", "Container", "WorkItem")]),
}

EDGE_TYPES: dict[str, type[BaseModel]] = {name: model for name, (model, _) in _SPECS.items()}

EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {}
for _name, (_, _pairs) in _SPECS.items():
    for _pair in _pairs:
        EDGE_TYPE_MAP.setdefault(_pair, []).append(_name)

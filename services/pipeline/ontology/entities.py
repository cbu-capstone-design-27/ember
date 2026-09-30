"""Core entity types for the Ember knowledge graph (EMBER-34).

Class name = node label, docstring = description shown to the extraction LLM,
fields = node attributes. Field names must not collide with Graphiti's reserved
node fields (uuid, name, group_id, labels, created_at, name_embedding, summary,
attributes); test_ontology.py enforces this.
"""

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    """Where a node came from. Every source-backed node carries these."""

    source: str = Field(description="Source system: github, gitlab, jira, slack or teams.")
    kind: str = Field(description="Kind within the source, e.g. issue, pull_request, message.")
    external_id: str = Field(description="Stable id in the source system (issue key, ts, node id).")
    url: str | None = Field(default=None, description="Permalink in the source system, if any.")


class Person(BaseModel):
    """A human contributor, resolved across their accounts on different sources."""

    email: str | None = Field(default=None, description="Primary email, if known.")


class Identity(SourceRef):
    """One account on one source (a GitHub login, Slack user id, Jira accountId).

    Resolves to a Person. Kept separate so a wrong merge can be undone.
    """

    handle: str | None = Field(default=None, description="Login, username or display handle.")


class Container(SourceRef):
    """Something that holds work or conversation: repository, project, channel, team, workspace."""


class WorkItem(SourceRef):
    """A tracked unit of work: an issue, ticket, story or epic."""

    status: str | None = Field(default=None, description="Current status as the source names it.")
    item_type: str | None = Field(default=None, description="Source issue type, e.g. Story, Bug.")


class Change(SourceRef):
    """A proposed or landed code change: a pull request, merge request or commit."""

    state: str | None = Field(default=None, description="opened, merged, closed, ...")
    source_branch: str | None = Field(default=None, description="Branch the change comes from.")
    target_branch: str | None = Field(default=None, description="Branch the change targets.")


class Conversation(SourceRef):
    """A thread or chat: a sequence of messages that belong together."""


class Message(SourceRef):
    """A single message in a conversation or a comment on a work item or change."""


class Decision(BaseModel):
    """A choice the team made, with its rationale. Later decisions can supersede earlier ones."""

    status: str | None = Field(default=None, description="proposed, accepted or superseded.")
    decided_on: str | None = Field(default=None, description="ISO date the decision was made.")


class Module(BaseModel):
    """A part of the codebase or product: a directory, package, service or component."""

    path: str | None = Field(default=None, description="Repo-relative path, if it maps to one.")


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (Person, Identity, Container, WorkItem, Change, Conversation, Message, Decision, Module)
}

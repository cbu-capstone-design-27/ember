from .base import MapContext, Mapped, Reject, Skip
from .github import map_github
from .jira import issue_index, map_jira
from .slack import map_slack

_MAPPERS = {"github": map_github, "jira": map_jira, "slack": map_slack}


def map_record(ctx: MapContext, envelope: dict) -> Mapped:
    mapper = _MAPPERS.get(envelope["type"])
    if mapper is None:
        raise Skip("unsupported_source")
    return mapper(ctx, envelope["body"])


__all__ = ["MapContext", "Mapped", "Reject", "Skip", "issue_index", "map_record"]

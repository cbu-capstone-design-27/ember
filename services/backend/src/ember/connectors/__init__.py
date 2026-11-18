"""Every source the backend knows. The controller builds each one's parts from its settings."""

from __future__ import annotations

from ember.connectors.base import Connector
from ember.connectors.github import GitHubConnector
from ember.connectors.gitlab import GitLabConnector
from ember.connectors.jira import JiraConnector
from ember.connectors.slack import SlackConnector

CONNECTORS: tuple[type[Connector], ...] = (GitHubConnector, GitLabConnector, JiraConnector, SlackConnector)

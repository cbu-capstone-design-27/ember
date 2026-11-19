"""backfill:jira: reads each connected site's issues, with their changelogs and comments, into raw_events
(ADR 0010 "Replay and backfill on connect", jira-webhooks.md "Known gaps and catch-up", EMBER-167). The
schedule, lock, cursors, rate-limit waits and disconnects are work/backfill.py's.

- **What.** For each project in the site's credential (`jql`, Connect Jira's `project in (<ids>)`), a search by
  oldest update, then for each issue: the issue (`fields=*all`), every page of its changelog, kept as
  `issue.changelog.histories` as `expand=changelog` nests it, and every page of its comments.
- **Rows.** Each issue and comment shaped as the webhook body for its latest change (`jira:issue_updated`,
  `comment_updated`), with delivery_id `<object id>@<version>` (jira-webhooks.md, delivery_id), so a webhook
  copy and a backfill copy of one version fold into one source_records row.
- **Progress.** Cursor `project:<id>` holds the `updated` of the last issue stored, so a stop or crash costs one
  issue. JQL dates are minutes in the consenting user's time zone, so the search asks for `updated >= "-<n>m"`,
  relative to now, reaching a minute before the cursor. RECONCILE_SINCE starts a project with no cursor at that
  date instead of its whole history.
- **Token.** Refreshed when due before each call (atlassian.py). A refresh Atlassian refuses disconnects the
  site; a disconnect stops the pass at the next call.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ember.connectors.jira.atlassian import LIMITS, SOURCE, OAuthClient, SiteCredentials, TokenRefused, api_origin, bearer_headers
from ember.connectors.jira.enrich import describe
from ember.connectors.jira.webhook import jira_time
from ember.core import http
from ember.core.errors import ConfigError
from ember.work.backfill import Backfill

PAGE = 100


class Disconnected(Exception):
    """The site was disconnected mid-pass; the pass stops and isn't counted as done."""


def jql(project: str, since: datetime | None, now: datetime) -> str:
    """The project's issues by oldest update, from a minute before `since`. Relative minutes need no time zone."""
    clause = f"project = {project}"
    if since is not None:
        clause += f' AND updated >= "-{max(1, math.ceil((now - since).total_seconds() / 60) + 1)}m"'
    return clause + " ORDER BY updated ASC, key ASC"


def issue_bodies(issue: dict, changelog: list[dict], comments: list[dict]) -> list[dict]:
    """The webhook bodies for an issue's latest change and each comment's, as describe() reads them."""
    issue = {**issue, "changelog": {"histories": changelog}}
    bodies = [{"webhookEvent": "jira:issue_updated", "timestamp": _millis(issue["fields"]["updated"]), "issue": issue}]
    reduced = {"id": issue["id"], "key": issue["key"], "self": issue["self"],
               "fields": {"summary": issue["fields"].get("summary")}}  # what a comment webhook carries
    return bodies + [{"webhookEvent": "comment_updated", "timestamp": _millis(comment["updated"]), "comment": comment,
                      "issue": reduced} for comment in comments]


def as_delivery(body: dict) -> tuple[str, str, datetime, dict]:
    _kind, object_id, version, _issues = describe(body)
    return f"{object_id}@{version}", body["webhookEvent"], jira_time(version), body


def _millis(value: str) -> int:
    return int(jira_time(value).timestamp() * 1000)


class JiraBackfill(Backfill):
    source = SOURCE
    limits_rule = LIMITS
    refused = TokenRefused
    account_stream = "site"

    def __init__(self, *args, client: OAuthClient | None, missing: str = "", since: datetime | None = None,
                 **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.client = client
        self.missing = missing  # what of the api ring and the OAuth client is unset, when one is
        self.since = since
        self.sites: SiteCredentials | None = None

    async def start(self) -> None:
        if self.missing:
            raise ConfigError(self.missing)
        await super().start()
        self.sites = SiteCredentials(self.engine, self.credentials.ring("api"), self.client, exchange=self.exchange)

    async def account(self, site: str, tenant: str, secret: dict, stopping: asyncio.Event) -> dict[str, Any]:
        def call(method: str, path: str, payload: dict | None = None):
            return self.patiently(stopping, lambda: self.call(site, tenant, method, path, payload), path.split("?")[0])

        projects = re.findall(r"\d+", secret["jql"])  # project in (<ids>), from Connect Jira
        rows = 0
        for project in projects:
            rows += await self.project(site, tenant, project, call)
        return {"projects": len(projects), "rows": rows}

    async def project(self, site: str, tenant: str, project: str, call) -> int:
        """Store the project's issues updated since its cursor, each with its changelog and comments. New rows."""
        stream = f"project:{project}"
        position = await self.cursor(tenant, stream)
        search = {"jql": jql(project, jira_time(position) if position else self.since, await self.now()),
                  "maxResults": PAGE, "fields": ["key"]}
        rows = 0
        while True:
            page = await call("POST", "/rest/api/3/search/jql", search)
            for found in page.get("issues") or []:
                key = found["key"]
                issue = await call("GET", f"/rest/api/3/issue/{key}?{urlencode({'fields': '*all'})}")
                changelog = await self.paged(call, f"/rest/api/3/issue/{key}/changelog", "values")
                comments = await self.paged(call, f"/rest/api/3/issue/{key}/comment", "comments")
                rows += await self.store(site, tenant, [as_delivery(body)
                                                        for body in issue_bodies(issue, changelog, comments)])
                await self.save_cursor(tenant, stream, issue["fields"]["updated"])
            if page.get("isLast", True) or not page.get("nextPageToken"):
                return rows
            search["nextPageToken"] = page["nextPageToken"]

    @staticmethod
    async def paged(call, path: str, field: str) -> list[dict]:
        """Every item of a startAt-paged list."""
        items: list[dict] = []
        while True:
            page = await call("GET", f"{path}?{urlencode({'startAt': len(items), 'maxResults': PAGE})}")
            batch = page.get(field) or []
            items += batch
            if page.get("isLast") or not batch or len(items) >= page.get("total", 0):
                return items

    async def call(self, site: str, tenant: str, method: str, path: str, payload: dict | None) -> Any:
        """One call to the site's REST API with its token, refreshed when due, through its rate_limits row."""
        assert self.sites is not None
        credential = await self.sites.fresh(site, tenant=tenant)
        if credential is None:
            raise Disconnected(f"site {site} was disconnected; stopped")
        body = None if payload is None else json.dumps(payload).encode()
        response = await asyncio.to_thread(
            http.exchange_with_retry, self.limits.exchange(tenant, f"https://{site}", self.exchange), method,
            api_origin(credential["cloud_id"]) + path, bearer_headers(credential["access_token"], json_body=body is not None),
            body, limits=LIMITS)
        return http.read_json(response, f"{method} {path.split('?')[0]}", LIMITS)

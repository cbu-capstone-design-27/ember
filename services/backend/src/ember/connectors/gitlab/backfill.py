"""backfill:gitlab: reads each connected group's projects into raw_events (gitlab-webhooks.md, "Known gaps
and catch-up", reconcile from REST). Replaces services/gitlab-ingestion/pull.py. The schedule, cursors,
limits and disconnects are work/backfill.py's.

- **What.** Every project in the group and its subgroups (archived ones skipped): the project, then its
  merge requests and issues updated since the project's cursor (`project:<id>`, the time the last read of it
  started), each with all its notes. A new note bumps its parent's `updated_at`, so new comments are
  covered. Confidential issues and internal notes are skipped, as the webhook triggers are. Commits and
  branches aren't read.
- **Rows.** The REST objects as GitLab returns them (a note gets its `project_id`), event name = kind,
  delivery_id `<object_id>@<version>`, occurred_at the object's `updated_at` (a project's
  `last_activity_at`). enrich:gitlab tells them from webhooks by their fields, and a webhook copy and a
  REST copy of one version fold into one source_records row.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote, urlencode

from ember.connectors.gitlab.enrich import (LIMITS, MAX_PAGES, PAGE_SIZE, SOURCE, TokenRefused, _time, api_root, get,
                                           next_link, stamp)
from ember.core.errors import Permanent
from ember.work.backfill import Backfill


class GitLabBackfill(Backfill):
    source = SOURCE
    limits_rule = LIMITS
    refused = TokenRefused
    account_stream = "group"

    async def account(self, account_id: str, tenant: str, secret: dict, stopping: asyncio.Event) -> dict:
        api, token = api_root(account_id, secret)
        group = account_id.split("/", 1)[1]

        async def each(path: str, **params):
            """Every object of a list, page by page, each request waiting out its rate limits."""
            url: str | None = f"{api}{path}?{urlencode({**params, 'per_page': PAGE_SIZE})}"
            for _ in range(MAX_PAGES):
                body, response = await self.patiently(
                    stopping, lambda: get(self.limits, self.exchange, tenant, account_id, token, url), path)
                for item in body:
                    yield item
                url = next_link(response.headers.get("Link") or response.headers.get("link"))
                if url is None:
                    return
                if not url.startswith(api + "/"):  # the token never goes anywhere else
                    raise Permanent(f"refusing a pagination link outside {api}")

        projects = rows = 0
        async for project in each(f"/groups/{quote(group, safe='')}/projects", include_subgroups="true",
                                  archived="false"):
            rows += await self.project(account_id, tenant, project, each)
            projects += 1
        return {"projects": projects, "rows": rows}

    async def project(self, account_id: str, tenant: str, project: dict, each) -> int:
        pid, stream = project["id"], f"project:{project['id']}"
        started, since = await self.now(), await self.cursor(tenant, stream)
        window = {"updated_after": since} if since else {}
        found = [(f"{pid}@{stamp(project['last_activity_at'])}", "project", project["last_activity_at"], project)]
        for kind, path, mark, extra in (("merge_request", "merge_requests", "!", {}),
                                        ("issue", "issues", "#", {"scope": "all"})):
            async for item in each(f"/projects/{pid}/{path}", state="all", order_by="created_at", sort="asc",
                                   **extra, **window):
                if item.get("confidential") is True:
                    continue
                found.append((f"{pid}{mark}{item['iid']}@{stamp(item['updated_at'])}", kind, item["updated_at"], item))
                async for note in each(f"/projects/{pid}/{path}/{item['iid']}/notes", sort="asc",
                                       order_by="created_at"):
                    if note.get("internal") is True or note.get("confidential") is True:
                        continue
                    note = {"project_id": pid, **note}
                    found.append((f"{pid}/note/{note['id']}@{stamp(note['updated_at'])}", "note",
                                  note["updated_at"], note))
        rows = await self.store(account_id, tenant, [(delivery_id, name, _time(when), body)
                                                     for delivery_id, name, when, body in found])
        await self.save_cursor(tenant, stream, started.isoformat())
        return rows

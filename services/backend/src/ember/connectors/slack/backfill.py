"""backfill:slack: reads each connected workspace's channel history into raw_events (slack-webhooks.md
"Known gaps and catch-up"). The schedule, cursors, limits and disconnects are work/backfill.py's.

- **What.** Every channel the bot is a member of (`users.conversations`): `conversations.history` since the
  channel's cursor (`channel:<id>`, the newest ts read, written when the channel is done), and
  `conversations.replies` for each thread in it. History and replies allow one call a minute for a
  non-Marketplace app.
- **Rows.** delivery_id `<channel>/<ts>@<version>` (slack-webhooks.md, delivery_id). An edited message is
  shaped as the `message_changed` delivery Slack sends for it, so a webhook copy and a backfill copy of one
  version fold into one source_records row.

Not covered yet: a reply posted since the last pass to a thread older than the channel's cursor (history only
returns newer parents). The live reply event covers it unless its delivery was lost.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from ember.connectors.slack.enrich import LIMITS, SOURCE, TokenRefused, api
from ember.connectors.slack.webhook import slack_time
from ember.work.backfill import Backfill, Stopped

__all__ = ["SlackBackfill", "Stopped", "as_delivery"]
PAGE = 200  # Slack caps a non-Marketplace app's history and replies at 15 a page whatever is asked


def as_delivery(team: str, channel: str, message: dict) -> tuple[str, str, datetime, dict]:
    """(delivery_id, event_name, occurred_at, body) for one message read from history or replies."""
    ts = message["ts"]
    edited = (message.get("edited") or {}).get("ts")
    if edited:  # as Slack delivers an edit: when it was edited, carrying the message as it is now
        event = {"type": "message", "subtype": "message_changed", "channel": channel, "ts": edited, "hidden": True,
                 "message": message}
    else:
        event = {**message, "type": "message", "channel": channel}
    name = "message" + (f":{event['subtype']}" if event.get("subtype") else "")
    body = {"type": "event_callback", "team_id": team, "event": event}
    return f"{channel}/{ts}@{edited or ts}", name, slack_time(edited or ts), body


class SlackBackfill(Backfill):
    source = SOURCE
    limits_rule = LIMITS
    refused = TokenRefused
    account_stream = "workspace"

    async def account(self, team: str, tenant: str, secret: dict, stopping: asyncio.Event) -> dict:
        token = secret.get("bot_token")
        if not token:
            raise TokenRefused(f"workspace {team} has no bot token")

        async def call(method: str, params: dict) -> dict:
            params = {k: v for k, v in params.items() if v != ""}
            return await self.patiently(stopping, lambda: api(self.limits, self.exchange, tenant, team, token,
                                                              method, params), method) or {}

        async def pages(method: str, params: dict):
            cursor = ""
            while True:
                page = await call(method, {**params, "limit": PAGE, "cursor": cursor})
                yield page
                cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
                if not cursor:
                    return

        rows = channels = 0
        async for page in pages("users.conversations", {"types": "public_channel,private_channel",
                                                        "exclude_archived": "true"}):
            for channel in page.get("channels") or []:
                stream = f"channel:{channel['id']}"
                oldest = await self.cursor(tenant, stream) or "0"
                newest = oldest
                async for history in pages("conversations.history", {"channel": channel["id"], "oldest": oldest}):
                    messages = history.get("messages") or []
                    rows += await self.store(team, tenant, [as_delivery(team, channel["id"], m) for m in messages])
                    for message in messages:
                        if message.get("reply_count") and message.get("thread_ts") == message.get("ts"):
                            async for replies in pages("conversations.replies",
                                                       {"channel": channel["id"], "ts": message["ts"]}):
                                rows += await self.store(team, tenant, [
                                    as_delivery(team, channel["id"], m) for m in replies.get("messages") or []
                                    if m.get("ts") != message["ts"]])  # the parent leads every page
                        if float(message["ts"]) > float(newest):
                            newest = message["ts"]
                await self.save_cursor(tenant, stream, newest)
                channels += 1
        return {"channels": channels, "rows": rows}

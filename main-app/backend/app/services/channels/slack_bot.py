"""
Slack Bot adapter — read channel history and post messages.

Uses the Slack Web API via httpx (no SDK).
Requires a Bot Token (xoxb-...) with scopes:
  channels:history, channels:read, chat:write, groups:history, groups:read
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()

SLACK_API = "https://slack.com/api"


@dataclass
class SlackMessage:
    """A single message from a Slack channel."""

    ts: str
    user: str
    text: str
    thread_ts: Optional[str] = None


class SlackBotAdapter:
    """Read from and write to Slack channels using Bot Token."""

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self._headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def test_auth(self) -> dict:
        """Verify the bot token is valid. Returns bot info."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SLACK_API}/auth.test", headers=self._headers
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "auth.test failed"))
            return data

    async def list_channels(self) -> list[dict]:
        """List public channels the bot is a member of."""
        channels: list[dict] = []
        cursor = ""
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                resp = await client.get(
                    f"{SLACK_API}/conversations.list",
                    headers=self._headers,
                    params={
                        "types": "public_channel,private_channel",
                        "exclude_archived": "true",
                        "limit": 200,
                        "cursor": cursor,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(data.get("error", "conversations.list failed"))
                for ch in data.get("channels", []):
                    channels.append({
                        "id": ch["id"],
                        "name": ch.get("name", ""),
                        "is_member": ch.get("is_member", False),
                    })
                cursor = data.get("response_metadata", {}).get("next_cursor", "")
                if not cursor:
                    break
        return channels

    async def fetch_history(
        self,
        channel_id: str,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> list[SlackMessage]:
        """Read messages from a channel since a given timestamp."""
        params: dict = {"channel": channel_id, "limit": limit}
        if since:
            params["oldest"] = str(since.timestamp())

        messages: list[SlackMessage] = []
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SLACK_API}/conversations.history",
                headers=self._headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "conversations.history failed"))
            for m in data.get("messages", []):
                if m.get("subtype"):
                    continue
                messages.append(
                    SlackMessage(
                        ts=m["ts"],
                        user=m.get("user", "unknown"),
                        text=m.get("text", ""),
                        thread_ts=m.get("thread_ts"),
                    )
                )
        return messages

    async def post_message(
        self,
        channel_id: str,
        text: str,
        thread_ts: Optional[str] = None,
    ) -> dict:
        """Post a message to a channel (optionally in a thread)."""
        payload: dict = {"channel": channel_id, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{SLACK_API}/chat.postMessage",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "chat.postMessage failed"))
            logger.info("slack_message_posted", channel=channel_id)
            return data

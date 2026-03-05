"""
Teams Bot adapter — read channel messages and post replies via Microsoft Graph.

Requires an Azure AD app with application permissions:
  ChannelMessage.Read.All, ChannelMessage.Send, Team.ReadBasic.All

Uses client-credentials flow (app-only), so no user login needed for reading.
For sending, the app must be installed in the team.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


@dataclass
class TeamsMessage:
    """A single message from a Teams channel."""

    message_id: str
    user: str
    text: str
    created: datetime


class TeamsBotAdapter:
    """Read from and write to Teams channels using Graph API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self._access_token: Optional[str] = None

    async def _get_app_token(self) -> str:
        """Obtain an application-only access token via client credentials."""
        if self._access_token:
            return self._access_token

        url = AUTH_URL.format(tenant=self.tenant_id)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            return self._access_token

    async def test_auth(self) -> dict:
        """Verify credentials by fetching the app service principal."""
        token = await self._get_app_token()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/organization",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def list_teams(self) -> list[dict]:
        """List all teams the app can see."""
        token = await self._get_app_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/teams",
                headers={"Authorization": f"Bearer {token}"},
                params={"$top": 100},
            )
            resp.raise_for_status()
            teams = resp.json().get("value", [])
        return [
            {"id": t["id"], "name": t.get("displayName", "")} for t in teams
        ]

    async def list_channels(self, team_id: str) -> list[dict]:
        """List channels in a team."""
        token = await self._get_app_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/teams/{team_id}/channels",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            channels = resp.json().get("value", [])
        return [
            {"id": ch["id"], "name": ch.get("displayName", "")} for ch in channels
        ]

    async def fetch_history(
        self,
        team_id: str,
        channel_id: str,
        since: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[TeamsMessage]:
        """Read messages from a Teams channel."""
        token = await self._get_app_token()
        url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages"
        params: dict = {"$top": limit, "$orderby": "createdDateTime desc"}
        if since:
            iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            params["$filter"] = f"createdDateTime ge {iso}"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            resp.raise_for_status()
            raw_messages = resp.json().get("value", [])

        messages: list[TeamsMessage] = []
        for m in raw_messages:
            body = m.get("body", {})
            text = body.get("content", "")
            user = m.get("from", {}).get("user", {}).get("displayName", "unknown")
            created_str = m.get("createdDateTime", "")
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                created = datetime.utcnow()

            messages.append(
                TeamsMessage(
                    message_id=m.get("id", ""),
                    user=user,
                    text=text,
                    created=created,
                )
            )
        return messages

    async def post_message(
        self,
        team_id: str,
        channel_id: str,
        text: str,
    ) -> dict:
        """Post a message to a Teams channel."""
        token = await self._get_app_token()
        url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "body": {"contentType": "html", "content": text},
                },
            )
            resp.raise_for_status()
            logger.info("teams_message_posted", team=team_id, channel=channel_id)
            return resp.json()

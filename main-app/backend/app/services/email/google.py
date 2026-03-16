"""
Google Workspace / Gmail adapter.

Requires a GCP OAuth 2.0 client with scopes:
  - https://www.googleapis.com/auth/gmail.modify
  - https://www.googleapis.com/auth/gmail.send
  - https://www.googleapis.com/auth/gmail.settings.basic

Push notifications use Gmail API watch() with a Cloud Pub/Sub topic.
"""

import base64
import json
from datetime import datetime
from email.mime.text import MIMEText
from typing import Optional

import httpx
import structlog

from app.services.email.base import EmailProviderBase, InboundEmail

logger = structlog.get_logger()

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleProvider(EmailProviderBase):
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    async def connect(self, oauth_code: str, redirect_uri: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": oauth_code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        async with httpx.AsyncClient() as client:
            profile = await client.get(
                f"{GMAIL_BASE}/users/me/profile",
                headers={"Authorization": f"Bearer {data['access_token']}"},
            )
            profile.raise_for_status()
            user_email = profile.json().get("emailAddress", "")

        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_in": data.get("expires_in", 3600),
            "user_email": user_email,
        }

    async def refresh_token(self, refresh_token: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_in": data.get("expires_in", 3600),
        }

    async def create_inbox(self, display_name: str, access_token: str) -> str:
        """Create a Gmail label to logically separate AI mail.

        Returns the authenticated user's email address (Gmail aliases
        need Google Admin SDK in production).
        """
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{GMAIL_BASE}/users/me/labels",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "name": f"AI-{display_name}",
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            profile = await client.get(
                f"{GMAIL_BASE}/users/me/profile",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            profile.raise_for_status()
            return profile.json().get("emailAddress", "")

    async def delete_inbox(self, email_address: str, access_token: str) -> None:
        logger.info("google_delete_inbox", address=email_address)

    async def fetch_new_messages(
        self, access_token: str, since: datetime
    ) -> list[InboundEmail]:
        epoch = int(since.timestamp())
        url = f"{GMAIL_BASE}/users/me/messages?q=after:{epoch}&maxResults=50"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            msg_ids = [m["id"] for m in resp.json().get("messages", [])]

            results: list[InboundEmail] = []
            for mid in msg_ids:
                detail = await client.get(
                    f"{GMAIL_BASE}/users/me/messages/{mid}?format=full",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                detail.raise_for_status()
                msg = detail.json()
                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                body_text = ""
                for part in msg.get("payload", {}).get("parts", []):
                    if part.get("mimeType") == "text/plain":
                        body_text = base64.urlsafe_b64decode(
                            part.get("body", {}).get("data", "")
                        ).decode("utf-8", errors="replace")
                        break
                if not body_text:
                    raw = msg.get("payload", {}).get("body", {}).get("data", "")
                    if raw:
                        body_text = base64.urlsafe_b64decode(raw).decode(
                            "utf-8", errors="replace"
                        )

                results.append(
                    InboundEmail(
                        provider_message_id=mid,
                        from_address=headers.get("from", ""),
                        to_address=headers.get("to", ""),
                        subject=headers.get("subject", ""),
                        body_text=body_text,
                        received_at=datetime.utcnow(),
                        in_reply_to=headers.get("message-id"),
                    )
                )
        return results

    async def send_reply(
        self,
        access_token: str,
        to: str,
        subject: str,
        body_html: str,
        in_reply_to: Optional[str] = None,
    ) -> None:
        mime = MIMEText(body_html, "html")
        mime["to"] = to
        mime["subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = in_reply_to

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GMAIL_BASE}/users/me/messages/send",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"raw": raw},
            )
            resp.raise_for_status()

    async def register_webhook(self, access_token: str, callback_url: str) -> str:
        """Gmail uses watch() with a Pub/Sub topic rather than a direct callback URL."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GMAIL_BASE}/users/me/watch",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "topicName": callback_url,
                    "labelIds": ["INBOX"],
                },
            )
            resp.raise_for_status()
            return resp.json().get("historyId", "")

    async def validate_webhook_payload(
        self, headers: dict, body: bytes
    ) -> Optional[InboundEmail]:
        try:
            data = json.loads(body)
            pubsub_data = data.get("message", {}).get("data", "")
            if pubsub_data:
                decoded = json.loads(base64.urlsafe_b64decode(pubsub_data))
                return InboundEmail(
                    provider_message_id=decoded.get("historyId", ""),
                    from_address="",
                    to_address=decoded.get("emailAddress", ""),
                    subject="",
                    body_text="",
                )
        except Exception:
            logger.warning("google_webhook_parse_error")
        return None

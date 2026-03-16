"""
Microsoft 365 / Exchange Online adapter using Microsoft Graph API.

Requires an Azure AD app registration with:
  - Mail.ReadWrite, Mail.Send, MailboxSettings.ReadWrite (delegated or application)
  - For shared-mailbox creation: admin-consented Directory permissions

Token exchange, message fetching, sending, and webhook subscriptions
all go through the Graph REST API via httpx.
"""

import json
from datetime import datetime
from typing import Optional

import httpx
import structlog

from app.services.email.base import EmailProviderBase, InboundEmail

logger = structlog.get_logger()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


class MicrosoftProvider(EmailProviderBase):
    def __init__(self, client_id: str, client_secret: str, tenant_id: str = "common"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id

    async def connect(self, oauth_code: str, redirect_uri: str) -> dict:
        url = f"{AUTH_URL.format(tenant=self.tenant_id)}/token"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": oauth_code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "scope": "https://graph.microsoft.com/.default offline_access",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # Fetch authenticated user email
        async with httpx.AsyncClient() as client:
            me = await client.get(
                f"{GRAPH_BASE}/me",
                headers={"Authorization": f"Bearer {data['access_token']}"},
            )
            me.raise_for_status()
            user_email = me.json().get("mail") or me.json().get("userPrincipalName", "")

        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_in": data.get("expires_in", 3600),
            "user_email": user_email,
        }

    async def refresh_token(self, refresh_token: str) -> dict:
        url = f"{AUTH_URL.format(tenant=self.tenant_id)}/token"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": "https://graph.microsoft.com/.default offline_access",
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
        """Create a mail-enabled distribution group or shared mailbox alias.

        For simplicity, we create an Outlook mail folder to act as a logical
        'inbox'. In production, use the Admin SDK to create a shared mailbox.
        Returns the user's primary email (the folder is a routing concept).
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GRAPH_BASE}/me/mailFolders",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"displayName": f"AI-{display_name}"},
            )
            resp.raise_for_status()
            me = await client.get(
                f"{GRAPH_BASE}/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            me.raise_for_status()
            return me.json().get("mail") or me.json().get("userPrincipalName", "")

    async def delete_inbox(self, email_address: str, access_token: str) -> None:
        logger.info("microsoft_delete_inbox", address=email_address)

    async def fetch_new_messages(
        self, access_token: str, since: datetime
    ) -> list[InboundEmail]:
        iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{GRAPH_BASE}/me/messages?$filter=receivedDateTime ge {iso}&$top=50&$orderby=receivedDateTime desc"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            messages = resp.json().get("value", [])

        results: list[InboundEmail] = []
        for m in messages:
            results.append(
                InboundEmail(
                    provider_message_id=m["id"],
                    from_address=m.get("from", {})
                    .get("emailAddress", {})
                    .get("address", ""),
                    to_address=(m.get("toRecipients") or [{}])[0]
                    .get("emailAddress", {})
                    .get("address", ""),
                    subject=m.get("subject", ""),
                    body_text=m.get("body", {}).get("content", "")
                    if m.get("body", {}).get("contentType") == "text"
                    else "",
                    body_html=m.get("body", {}).get("content", "")
                    if m.get("body", {}).get("contentType") == "html"
                    else None,
                    received_at=datetime.fromisoformat(
                        m["receivedDateTime"].replace("Z", "+00:00")
                    ),
                    in_reply_to=m.get("internetMessageId"),
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
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GRAPH_BASE}/me/sendMail",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()

    async def register_webhook(self, access_token: str, callback_url: str) -> str:
        payload = {
            "changeType": "created",
            "notificationUrl": callback_url,
            "resource": "me/mailFolders('Inbox')/messages",
            "expirationDateTime": "2026-03-21T00:00:00Z",
            "clientState": "retrace-email-webhook",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GRAPH_BASE}/subscriptions",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["id"]

    async def validate_webhook_payload(
        self, headers: dict, body: bytes
    ) -> Optional[InboundEmail]:
        try:
            data = json.loads(body)
            for notification in data.get("value", []):
                resource = notification.get("resource", "")
                if "messages" in resource.lower():
                    return InboundEmail(
                        provider_message_id=notification.get("resourceData", {}).get(
                            "id", ""
                        ),
                        from_address="",
                        to_address="",
                        subject="",
                        body_text="",
                    )
        except Exception:
            logger.warning("microsoft_webhook_parse_error")
        return None

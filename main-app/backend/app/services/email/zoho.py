"""
Zoho Mail adapter.

Requires a Zoho API Console self-client or server-based app with scopes:
  - ZohoMail.messages.ALL
  - ZohoMail.accounts.READ

Uses Zoho Mail API v1 for message operations.
"""

import json
from datetime import datetime
from typing import Optional

import httpx
import structlog

from app.services.email.base import EmailProviderBase, InboundEmail

logger = structlog.get_logger()

ZOHO_MAIL_BASE = "https://mail.zoho.com/api"
ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"


class ZohoProvider(EmailProviderBase):
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    async def connect(self, oauth_code: str, redirect_uri: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                ZOHO_TOKEN_URL,
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

        access_token = data["access_token"]
        async with httpx.AsyncClient() as client:
            accounts = await client.get(
                f"{ZOHO_MAIL_BASE}/accounts",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            )
            accounts.raise_for_status()
            account_data = accounts.json().get("data", [{}])
            user_email = account_data[0].get("primaryEmailAddress", "") if account_data else ""

        return {
            "access_token": access_token,
            "refresh_token": data.get("refresh_token", ""),
            "expires_in": data.get("expires_in", 3600),
            "user_email": user_email,
            "account_id": account_data[0].get("accountId", "") if account_data else "",
        }

    async def refresh_token(self, refresh_token: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                ZOHO_TOKEN_URL,
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
        """Zoho doesn't support programmatic alias creation via public API.

        Returns the primary email. In production, admin would create an
        alias/group via Zoho Admin Console and register it here.
        """
        async with httpx.AsyncClient() as client:
            accounts = await client.get(
                f"{ZOHO_MAIL_BASE}/accounts",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            )
            accounts.raise_for_status()
            account_data = accounts.json().get("data", [{}])
            return account_data[0].get("primaryEmailAddress", "") if account_data else ""

    async def delete_inbox(self, email_address: str, access_token: str) -> None:
        logger.info("zoho_delete_inbox", address=email_address)

    async def fetch_new_messages(
        self, access_token: str, since: datetime
    ) -> list[InboundEmail]:
        """Fetch unread messages from Inbox received in the last 5 minutes."""
        async with httpx.AsyncClient() as client:
            accounts = await client.get(
                f"{ZOHO_MAIL_BASE}/accounts",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            )
            accounts.raise_for_status()
            account_data = accounts.json().get("data", [])
            if not account_data:
                return []
            account_id = account_data[0]["accountId"]

            # Get Inbox folder ID (optional; needs ZohoMail.folders.READ)
            inbox_folder_id = None
            try:
                folders_resp = await client.get(
                    f"{ZOHO_MAIL_BASE}/accounts/{account_id}/folders",
                    headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
                )
                folders_resp.raise_for_status()
                folders = folders_resp.json().get("data", [])
                for f in folders:
                    if (f.get("folderName") or "").lower() == "inbox":
                        inbox_folder_id = f.get("folderId")
                        break
                if not inbox_folder_id and folders:
                    inbox_folder_id = folders[0].get("folderId")
            except Exception:
                pass

            params = {
                "limit": 50,
                "sortorder": "false",  # newest first
                "status": "unread",
                "includeto": "true",   # include toAddress in list response
            }
            if inbox_folder_id is not None:
                params["folderId"] = inbox_folder_id

            resp = await client.get(
                f"{ZOHO_MAIL_BASE}/accounts/{account_id}/messages/view",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
                params=params,
            )
            resp.raise_for_status()
            messages = resp.json().get("data", [])

        results: list[InboundEmail] = []
        for m in messages:
            received_ts = m.get("receivedTime", 0)
            try:
                ts = int(received_ts)
                epoch_seconds = ts / 1000 if ts > 1e12 else ts
                received_at = datetime.utcfromtimestamp(epoch_seconds)
            except (TypeError, ValueError, OSError):
                received_at = since
            if received_at < since:
                continue
            to_raw = (m.get("toAddress", "") or "").replace("&lt;", "<").replace("&gt;", ">")
            results.append(
                InboundEmail(
                    provider_message_id=m.get("messageId", ""),
                    from_address=m.get("fromAddress", ""),
                    to_address=to_raw,
                    subject=m.get("subject", ""),
                    body_text=m.get("content", ""),
                    received_at=received_at,
                )
            )
        logger.info("zoho_fetch_unread", count=len(messages), after_time_filter=len(results), since=since.isoformat())
        return results

    async def fetch_unread_emails(self, access_token: str, limit: int = 5) -> list[dict]:
        """Fetch most recent unread emails directly from Zoho, no time filter.

        Returns raw dicts with from_address, subject, received_at, body_text.
        """
        async with httpx.AsyncClient() as client:
            acct_resp = await client.get(
                f"{ZOHO_MAIL_BASE}/accounts",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            )
            logger.info("zoho_unread_accounts_status", status=acct_resp.status_code)
            acct_resp.raise_for_status()
            acct_json = acct_resp.json()
            account_data = acct_json.get("data", [])
            if not account_data:
                logger.warning("zoho_unread_no_accounts", raw=acct_json)
                return []
            account_id = account_data[0]["accountId"]
            logger.info("zoho_unread_account", account_id=account_id)

            params: dict = {
                "limit": str(limit),
                "sortorder": "false",
                "status": "unread",
                "includeto": "true",
            }

            url = f"{ZOHO_MAIL_BASE}/accounts/{account_id}/messages/view"
            logger.info("zoho_unread_request", url=url, params=params)
            resp = await client.get(
                url,
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
                params=params,
            )
            logger.info("zoho_unread_response_status", status=resp.status_code)
            raw = resp.json()
            logger.info("zoho_unread_raw_response_keys", keys=list(raw.keys()), status_field=raw.get("status", {}) if isinstance(raw.get("status"), dict) else raw.get("status"))
            resp.raise_for_status()

            messages = raw.get("data", [])
            logger.info("zoho_unread_message_count", count=len(messages))
            if messages:
                first = messages[0]
                logger.info("zoho_unread_first_msg_keys", keys=list(first.keys()), subject=first.get("subject", ""), fromAddress=first.get("fromAddress", ""))

        results = []
        for m in messages:
            received_ts = m.get("receivedTime", 0)
            try:
                ts = int(received_ts)
                received_at = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts)
            except (TypeError, ValueError):
                received_at = datetime.utcnow()
            results.append({
                "from_address": m.get("fromAddress", ""),
                "subject": m.get("subject", ""),
                "received_at": received_at.isoformat(),
                "body_text": (m.get("content") or m.get("summary") or "")[:500],
            })
        return results

    async def send_reply(
        self,
        access_token: str,
        to: str,
        subject: str,
        body_html: str,
        in_reply_to: Optional[str] = None,
    ) -> None:
        async with httpx.AsyncClient() as client:
            accounts = await client.get(
                f"{ZOHO_MAIL_BASE}/accounts",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            )
            accounts.raise_for_status()
            account_data = accounts.json().get("data", [])
            if not account_data:
                raise RuntimeError("No Zoho account found")
            account_id = account_data[0]["accountId"]
            from_email = account_data[0].get("primaryEmailAddress", "")

            resp = await client.post(
                f"{ZOHO_MAIL_BASE}/accounts/{account_id}/messages",
                headers={
                    "Authorization": f"Zoho-oauthtoken {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "fromAddress": from_email,
                    "toAddress": to,
                    "subject": subject,
                    "content": body_html,
                    "mailFormat": "html",
                },
            )
            resp.raise_for_status()

    async def register_webhook(self, access_token: str, callback_url: str) -> str:
        """Zoho Mail doesn't support push webhooks natively.

        Polling via fetch_new_messages is the primary mechanism.
        Returns an empty string as a no-op subscription id.
        """
        logger.info("zoho_webhook_not_supported", note="use polling instead")
        return ""

    async def validate_webhook_payload(
        self, headers: dict, body: bytes
    ) -> Optional[InboundEmail]:
        return None

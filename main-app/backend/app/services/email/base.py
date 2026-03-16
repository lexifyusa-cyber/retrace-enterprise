"""
Abstract base for email provider adapters.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class InboundEmail:
    """Parsed inbound email message from any provider."""

    provider_message_id: str
    from_address: str
    to_address: str
    subject: str
    body_text: str
    body_html: Optional[str] = None
    received_at: datetime = field(default_factory=datetime.utcnow)
    in_reply_to: Optional[str] = None
    headers: dict = field(default_factory=dict)


class EmailProviderBase(ABC):
    """Interface that every email vendor adapter must implement."""

    @abstractmethod
    async def connect(self, oauth_code: str, redirect_uri: str) -> dict:
        """Exchange an OAuth authorization code for tokens.

        Returns a dict with at minimum:
          - access_token, refresh_token, expires_at
          - user_email (the authenticated mailbox owner)
        """

    @abstractmethod
    async def refresh_token(self, refresh_token: str) -> dict:
        """Refresh an expired access token. Returns updated token dict."""

    @abstractmethod
    async def create_inbox(self, display_name: str, access_token: str) -> str:
        """Provision a mailbox / alias for AI use. Returns the email address."""

    @abstractmethod
    async def delete_inbox(self, email_address: str, access_token: str) -> None:
        """Remove a previously created mailbox / alias."""

    @abstractmethod
    async def fetch_new_messages(
        self, access_token: str, since: datetime
    ) -> list[InboundEmail]:
        """Poll for messages received after *since*."""

    @abstractmethod
    async def send_reply(
        self,
        access_token: str,
        to: str,
        subject: str,
        body_html: str,
        in_reply_to: Optional[str] = None,
    ) -> None:
        """Send (or reply to) an email."""

    @abstractmethod
    async def register_webhook(
        self, access_token: str, callback_url: str
    ) -> str:
        """Register a push-notification subscription. Returns subscription_id."""

    @abstractmethod
    async def validate_webhook_payload(
        self, headers: dict, body: bytes
    ) -> Optional[InboundEmail]:
        """Validate and parse an inbound webhook payload. Returns None on invalid."""

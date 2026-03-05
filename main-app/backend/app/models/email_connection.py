"""
Email connection model — stores OAuth credentials for a company's email provider.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class EmailConnection(Base):
    """OAuth connection to an email provider (Microsoft / Google / Zoho)."""

    __tablename__ = "email_connections"

    connection_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "microsoft" | "google" | "zoho"
    tenant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_id: Mapped[Optional[str]] = mapped_column(String(512))  # for OAuth refresh
    client_secret: Mapped[Optional[str]] = mapped_column(Text)  # for OAuth refresh
    oauth_token_encrypted: Mapped[Optional[str]] = mapped_column(
        Text
    )  # encrypted JSON blob
    status: Mapped[str] = mapped_column(
        String(20), default="active"
    )  # "active" | "expired" | "error"
    webhook_subscription_id: Mapped[Optional[str]] = mapped_column(String(255))
    slack_webhook_url: Mapped[Optional[str]] = mapped_column(Text)
    teams_webhook_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    inboxes = relationship(
        "EmailInbox", back_populates="connection", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "connection_id": self.connection_id,
            "provider": self.provider,
            "tenant_name": self.tenant_name,
            "status": self.status,
            "slack_webhook_url": self.slack_webhook_url,
            "teams_webhook_url": self.teams_webhook_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

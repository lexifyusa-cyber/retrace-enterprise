"""
Email inbox model — links a unique AI email address to a product.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class EmailInbox(Base):
    """Per-product AI inbox with action-mode configuration."""

    __tablename__ = "email_inboxes"

    inbox_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.product_id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("email_connections.connection_id", ondelete="CASCADE"),
        nullable=False,
    )
    email_address: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    action_config: Mapped[Optional[dict]] = mapped_column(
        JSON,
        default=lambda: {
            "auto_reply": False,
            "auto_reply_confidence_threshold": 0.9,
            "draft_and_review": True,
            "classify_and_route": False,
            "summarize_and_notify": False,
            "escalation_channel": "slack",
            "slack_webhook_url": None,
            "teams_webhook_url": None,
            "route_rules": [],
        },
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    connection = relationship("EmailConnection", back_populates="inboxes")
    messages = relationship(
        "EmailMessage", back_populates="inbox", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "inbox_id": self.inbox_id,
            "product_id": self.product_id,
            "connection_id": self.connection_id,
            "email_address": self.email_address,
            "is_active": self.is_active,
            "action_config": self.action_config or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

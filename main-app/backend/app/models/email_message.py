"""
Email message model — audit trail for every inbound email and its processing outcome.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class EmailMessage(Base):
    """Record of an inbound email and all actions taken on it."""

    __tablename__ = "email_messages"

    message_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    inbox_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("email_inboxes.inbox_id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        nullable=False,
    )
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[Optional[str]] = mapped_column(Text)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(512))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(
        String(30), default="processing"
    )  # processing | auto_replied | drafted | classified | escalated | approved | rejected
    ai_response: Mapped[Optional[str]] = mapped_column(Text)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    classification: Mapped[Optional[dict]] = mapped_column(JSON)
    actions_taken: Mapped[Optional[dict]] = mapped_column(JSON, default=list)

    inbox = relationship("EmailInbox", back_populates="messages")

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "inbox_id": self.inbox_id,
            "product_id": self.product_id,
            "from_address": self.from_address,
            "subject": self.subject,
            "body_text": self.body_text,
            "provider_message_id": self.provider_message_id,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "status": self.status,
            "ai_response": self.ai_response,
            "confidence_score": self.confidence_score,
            "classification": self.classification,
            "actions_taken": self.actions_taken or [],
        }

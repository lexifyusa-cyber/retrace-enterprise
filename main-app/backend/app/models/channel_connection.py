"""
Channel connection model — links a Slack or Teams channel to a product.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ChannelConnection(Base):
    """Slack / Teams channel linked to a product for reading + responding."""

    __tablename__ = "channel_connections"

    connection_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    product_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "slack" | "teams"
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(255), nullable=False)
    bot_token_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    # Teams-specific: team_id is needed alongside channel_id
    team_id: Mapped[Optional[str]] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_respond: Mapped[bool] = mapped_column(Boolean, default=False)
    ingest_history: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "connection_id": self.connection_id,
            "product_id": self.product_id,
            "platform": self.platform,
            "channel_name": self.channel_name,
            "channel_id": self.channel_id,
            "team_id": self.team_id,
            "is_active": self.is_active,
            "auto_respond": self.auto_respond,
            "ingest_history": self.ingest_history,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

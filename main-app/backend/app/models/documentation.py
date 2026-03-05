"""
Documentation (Knowledge Article) database model.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Documentation(Base):
    """A generated Knowledge Article / documentation page."""

    __tablename__ = "documentation"

    doc_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    product_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    doc_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    doc_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    doc_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="knowledge-article"
    )
    source_conversation_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft"
    )  # draft | approved
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "product_id": self.product_id,
            "title": self.title,
            "goal": self.goal,
            "doc_json": self.doc_json,
            "doc_markdown": self.doc_markdown,
            "doc_type": self.doc_type,
            "source_conversation_id": self.source_conversation_id,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

"""
Query History database model.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, Float, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class QueryHistory(Base):
    """Query History model - stores query history for analytics."""
    
    __tablename__ = "query_history"
    
    query_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4())
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    scope_filter: Mapped[Optional[dict]] = mapped_column(JSON)
    answer: Mapped[Optional[str]] = mapped_column(Text)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    sources: Mapped[Optional[dict]] = mapped_column(JSON)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "query_id": self.query_id,
            "user_id": self.user_id,
            "question": self.question,
            "scope_filter": self.scope_filter,
            "answer": self.answer,
            "confidence_score": self.confidence_score,
            "sources": self.sources,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

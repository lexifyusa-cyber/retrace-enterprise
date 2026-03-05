"""
Agent session database model — persists agent execution history.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, DateTime, Text, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AgentSession(Base):
    """Tracks each agent task execution for history and debugging."""

    __tablename__ = "agent_sessions"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    task: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="running"
    )  # running | completed | failed | stopped
    thread_id: Mapped[str] = mapped_column(String(36), nullable=False)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    messages_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_usage_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    final_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "product_id": self.product_id,
            "task": self.task,
            "status": self.status,
            "thread_id": self.thread_id,
            "iterations": self.iterations,
            "final_answer": self.final_answer,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

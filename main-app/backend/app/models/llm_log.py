"""
LLM log database model — persists every LLM request/response with token usage.

Used by the developer-mode LLM Logs viewer to inspect exactly what was sent
to the LLM for each agent iteration, along with token consumption.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, DateTime, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class LLMLog(Base):
    """One row per LLM invocation (per iteration, per conversation)."""

    __tablename__ = "llm_logs"

    log_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    iteration: Mapped[int] = mapped_column(Integer, default=0)

    # Full request payload sent to the LLM (JSON string of message list)
    request_messages_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Full LLM response content (raw text)
    response_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Token usage (standard + provider-specific)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)

    # Model metadata
    model_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Performance
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    # Any error during the LLM call
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )

    def to_dict(self) -> dict:
        return {
            "log_id": self.log_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "iteration": self.iteration,
            "request_messages": self.request_messages_json,
            "response_content": self.response_content,
            "tokens": {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "total": self.total_tokens,
                "cache_read": self.cache_read_tokens,
                "cache_creation": self.cache_creation_tokens,
                "reasoning": self.reasoning_tokens,
            },
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

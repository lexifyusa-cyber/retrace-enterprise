"""
Terminal session model — tracks PTY sessions bound to conversations.

The actual PTY process is ephemeral (in-memory in pty_manager), but this
model records metadata so the frontend can check whether a terminal exists
and provide the correct UX on reconnect after server restarts.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class TerminalSession(Base):
    """One-to-one mapping between a conversation and its PTY shell."""

    __tablename__ = "terminal_sessions"

    session_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    shell: Mapped[str] = mapped_column(String(100), nullable=False, default="/bin/zsh")
    initial_cwd: Mapped[str] = mapped_column(String(500), nullable=False, default="~")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "shell": self.shell,
            "initial_cwd": self.initial_cwd,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_active_at": self.last_active_at.isoformat() if self.last_active_at else None,
        }

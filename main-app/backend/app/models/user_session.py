"""
Server-side session model for token management and revocation.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class UserSession(Base):
    __tablename__ = "user_sessions"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # External auth tokens (OAuth / LDAP pass-through for MCP extensions)
    external_access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    external_id_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    external_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    external_token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    external_username: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    external_password: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

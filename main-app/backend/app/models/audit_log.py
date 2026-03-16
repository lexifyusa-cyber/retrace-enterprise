"""
Immutable audit log — append-only record of all sensitive actions.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    log_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    actor_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # login, login_failed, logout, role_changed, access_granted, access_revoked,
    # user_invited, user_deactivated, product_created, product_deleted,
    # tenant_config_changed, auth_config_changed
    target_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # user, product, tenant, auth_config, session
    target_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="web")  # web, api, system

    def to_dict(self) -> dict:
        return {
            "log_id": self.log_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "tenant_id": self.tenant_id,
            "actor_user_id": self.actor_user_id,
            "actor_email": self.actor_email,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "details": self.details,
            "ip_address": self.ip_address,
            "source": self.source,
        }

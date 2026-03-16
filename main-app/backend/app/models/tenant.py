"""
Tenant (Organization) model — the top-level isolation boundary.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, JSON, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, suspended, trial
    auth_method: Mapped[str] = mapped_column(String(20), default="email")  # email, ldap, azure_ad, google

    # Auth provider configs (encrypted JSON in production)
    ldap_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ldap_require_ssl: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    azure_ad_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    google_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Policies
    mfa_required: Mapped[bool] = mapped_column(Boolean, default=False)
    session_timeout_minutes: Mapped[int] = mapped_column(Integer, default=480)
    password_policy: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip_allowlist: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip_denylist: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    max_products_per_user_admin: Mapped[int] = mapped_column(Integer, default=10)
    max_managed_users_per_user_admin: Mapped[int] = mapped_column(Integer, default=50)  # default max users a user_admin can create

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "domain": self.domain,
            "status": self.status,
            "auth_method": self.auth_method,
            "ldap_require_ssl": getattr(self, "ldap_require_ssl", False),
            "mfa_required": self.mfa_required,
            "session_timeout_minutes": self.session_timeout_minutes,
            "max_products_per_user_admin": self.max_products_per_user_admin,
            "max_managed_users_per_user_admin": getattr(self, "max_managed_users_per_user_admin", 50),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

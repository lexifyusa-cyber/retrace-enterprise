"""
User model — supports email, LDAP, Azure AD, and Google auth.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, JSON, Boolean, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    hashed_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    role: Mapped[str] = mapped_column(String(30), default="user")
    # zero_admin | admin | user_admin | user

    auth_provider: Mapped[str] = mapped_column(String(20), default="email")
    # email | ldap | azure_ad | google

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret_encrypted: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_login_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    invited_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    force_password_change: Mapped[bool] = mapped_column(Boolean, default=False)
    max_products: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # per-user limit for user_admin; null = use tenant default
    max_managed_users: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # how many users a user_admin can create; null = use tenant default
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    department: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    timezone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    manager_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    employee_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    password_reset_token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    password_reset_expires: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    preferences: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "email": self.email,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "auth_provider": self.auth_provider,
            "phone": self.phone,
            "department": self.department,
            "timezone": self.timezone,
            "manager_id": self.manager_id,
            "employee_id": self.employee_id,
            "status": self.status,
            "is_active": self.is_active,
            "mfa_enabled": self.mfa_enabled,
            "force_password_change": self.force_password_change,
            "max_products": self.max_products,
            "max_managed_users": self.max_managed_users,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "locked_until": self.locked_until.isoformat() if self.locked_until else None,
            "last_login_ip": self.last_login_ip,
            "password_changed_at": self.password_changed_at.isoformat() if self.password_changed_at else None,
            "preferences": self.preferences or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

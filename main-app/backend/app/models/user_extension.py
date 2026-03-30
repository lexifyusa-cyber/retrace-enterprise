"""
Per-user MCP extension installs — each user has their own set of installed
extensions from the admin-managed catalog (mcp_extensions table).
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, String, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class UserExtension(Base):
    """A user's personal installation of a catalog MCP extension."""

    __tablename__ = "user_extensions"
    __table_args__ = (
        UniqueConstraint("extension_id", "user_id", name="uq_user_extension"),
    )

    user_extension_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    extension_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # User-specific overrides (merged with catalog defaults at runtime)
    env_vars_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    auth_config_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    installed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "user_extension_id": self.user_extension_id,
            "extension_id": self.extension_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "is_enabled": self.is_enabled,
            "env_vars_json": self._mask_env_secrets(),
            "auth_config_json": self.auth_config_json,
            "installed_at": self.installed_at.isoformat() if self.installed_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def _mask_env_secrets(self) -> dict | None:
        if not self.env_vars_json:
            return self.env_vars_json
        masked = {}
        sensitive_keywords = ("password", "secret", "token", "key", "credential")
        for k, v in self.env_vars_json.items():
            if any(kw in k.lower() for kw in sensitive_keywords) and v:
                masked[k] = "••••••••"
            else:
                masked[k] = v
        return masked

    def get_raw_env_vars(self) -> dict:
        return self.env_vars_json or {}

    def get_merged_env_vars(self, catalog_env_vars: dict | None) -> dict:
        """Merge catalog defaults with user overrides (user wins)."""
        merged = dict(catalog_env_vars or {})
        merged.update(self.env_vars_json or {})
        return merged

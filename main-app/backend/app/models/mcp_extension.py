"""
MCP Extension model — tracks externally connected MCP servers (like Claude Desktop extensions).

This is separate from the McpServer model which tracks *generated* MCP server code.
McpExtension tracks *running/connected* MCP server instances that the agent can use.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, String, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class McpExtension(Base):
    """A configured MCP server extension that the ReTrace agent can connect to."""

    __tablename__ = "mcp_extensions"

    extension_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # How to spawn the MCP server subprocess
    command: Mapped[str] = mapped_column(String(500), nullable=False)  # e.g. "python"
    args_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # e.g. ["-m", "servicenow_mcp"]
    env_vars_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # env vars passed to subprocess

    # Authentication configuration
    auth_type: Mapped[str] = mapped_column(
        String(30), default="none"
    )  # none | basic | oauth_passthrough | api_key
    auth_config_json: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True
    )  # e.g. {"token_env_var": "SERVICENOW_OAUTH_TOKEN", "token_source": "azure_ad"}

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    mcp_server_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # links to McpServer if built via MCP Builder
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "extension_id": self.extension_id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "command": self.command,
            "args_json": self.args_json,
            "env_vars_json": self._mask_env_secrets(),
            "auth_type": self.auth_type,
            "auth_config_json": self.auth_config_json,
            "is_enabled": self.is_enabled,
            "is_builtin": self.is_builtin,
            "mcp_server_id": self.mcp_server_id,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def _mask_env_secrets(self) -> dict | None:
        """Mask sensitive env var values (passwords, tokens, keys) in API responses."""
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
        """Get unmasked env vars for subprocess spawning."""
        return self.env_vars_json or {}

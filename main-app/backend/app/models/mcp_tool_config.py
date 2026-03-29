"""
MCP Tool Configuration model — stores user-configured MCP servers
that can be used as tools in Agent Chat.

Stores the raw mcpServers JSON config as-is for maximum compatibility
with any MCP server configuration format.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class McpToolConfig(Base):
    """User-configured MCP server that provides tools to Agent Chat."""

    __tablename__ = "mcp_tool_configs"

    config_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    # Display name (the key from mcpServers JSON, e.g. "github")
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Raw config JSON — stored exactly as provided (command, args, env, url, headers, etc.)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "config_id": self.config_id,
            "name": self.name,
            "config_json": self.config_json or {},
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

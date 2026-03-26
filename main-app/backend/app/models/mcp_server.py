"""
MCP Server registry model — tracks generated MCP servers.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class McpServer(Base):
    """Tracks MCP servers built by the MCP Builder."""

    __tablename__ = "mcp_servers"

    server_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    destination_folder: Mapped[str] = mapped_column(String(500), nullable=False)
    module_name: Mapped[str] = mapped_column(String(200), nullable=False)
    mcp_config_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    quick_start_commands: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    selected_endpoints_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    source_type: Mapped[str] = mapped_column(
        String(30), default="external_url"
    )  # internal | external_url | external_text | external_upload
    api_docs_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    api_base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    auth_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    kb_product_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "server_id": self.server_id,
            "name": self.name,
            "product_name": self.product_name,
            "destination_folder": self.destination_folder,
            "module_name": self.module_name,
            "mcp_config_json": self.mcp_config_json,
            "quick_start_commands": self.quick_start_commands,
            "selected_endpoints_json": self.selected_endpoints_json,
            "source_type": self.source_type,
            "api_docs_url": self.api_docs_url,
            "api_base_url": self.api_base_url,
            "auth_type": self.auth_type,
            "kb_product_id": self.kb_product_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

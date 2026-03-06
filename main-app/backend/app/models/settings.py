"""
Settings database model.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class LLMSettingsModel(Base):
    """LLM Settings database model."""
    
    __tablename__ = "llm_settings"
    
    # Single row - use id=1 always
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    
    # Chat model settings
    api_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Encrypted in production
    model_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider: Mapped[str] = mapped_column(String(50), default="openai")
    
    # Embeddings settings
    embeddings_api_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    embeddings_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    embeddings_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    
    # ScreenOps (Computer Use) — Anthropic API key and model for Claude coordinate finder
    screenops_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    screenops_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Agent tools — JSON {"disabled": ["screenops", "basic_search"]} for user-enabled/disabled tools
    agent_tools_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Training debug logging — when True, pipeline logs included/excluded folders and files
    debug_logging: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    # Training Phase 3: max number of files to extract in parallel (1 = sequential)
    max_parallel_files: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=1)

    # Metadata
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "api_url": self.api_url,
            "api_key": self.api_key,
            "model_name": self.model_name,
            "provider": self.provider,
            "embeddings_api_url": self.embeddings_api_url,
            "embeddings_api_key": self.embeddings_api_key,
            "embeddings_model": self.embeddings_model,
            "screenops_api_key": self.screenops_api_key,
            "screenops_model": self.screenops_model,
            "agent_tools_config": self.agent_tools_config,
            "debug_logging": bool(self.debug_logging) if self.debug_logging is not None else False,
            "max_parallel_files": self.max_parallel_files if self.max_parallel_files is not None else 1,
        }

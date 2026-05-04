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
    
    # ScreenOps (Computer Use) — URL, model, and API key for coordinate finder (OpenAI-compatible API)
    screenops_api_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    screenops_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    screenops_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Keyboard-only mode: seconds to wait when a mouse click is unavoidable and no coord finder
    screenops_mouse_timeout: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=30)
    # ScreenOps screenshot scale 25–100: percentage of original size sent to vision model (reduces tokens)
    screenops_image_scale: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=100)
    # Central delay (ms) before every screenshot capture — lets dynamic content settle before screenshot
    screenshot_delay_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=2000)
    
    # Web Search — Serper API key for enhanced web search
    serper_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Agent tools — JSON {"disabled": ["screenops", "web_search"]} for user-enabled/disabled tools
    agent_tools_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Agent max tool-use iterations per task (1–50, default 10)
    agent_max_iterations: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=10)

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
            "screenops_api_url": self.screenops_api_url,
            "screenops_api_key": self.screenops_api_key,
            "screenops_model": self.screenops_model,
            "screenops_mouse_timeout": self.screenops_mouse_timeout if self.screenops_mouse_timeout is not None else 30,
            "screenops_image_scale": self.screenops_image_scale if self.screenops_image_scale is not None else 100,
            "screenshot_delay_ms": self.screenshot_delay_ms if self.screenshot_delay_ms is not None else 2000,
            "serper_api_key": self.serper_api_key,
            "agent_tools_config": self.agent_tools_config,
            "agent_max_iterations": self.agent_max_iterations if self.agent_max_iterations is not None else 10,
            "debug_logging": bool(self.debug_logging) if self.debug_logging is not None else False,
            "max_parallel_files": self.max_parallel_files if self.max_parallel_files is not None else 1,
        }

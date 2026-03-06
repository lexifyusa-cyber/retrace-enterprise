"""
Application configuration using Pydantic Settings.
"""

import os
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

# Calculate absolute database path to ensure persistence
_backend_dir = Path(__file__).parent.parent.parent  # app/core/config.py -> app -> main-app/backend
_db_path = _backend_dir / "retrace.db"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )
    
    # Application
    APP_NAME: str = "ReTrace"
    APP_VERSION: str = "0.1.0"
    APP_ENV: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = True
    
    # Database
    # Use absolute path to ensure database persists across restarts
    DATABASE_URL: str = f"sqlite+aiosqlite:///{_db_path.absolute()}"
    
    # Authentication
    JWT_SECRET: str = "change-this-in-production-super-secret-key"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    MAX_FAILED_LOGINS: int = 5
    LOCKOUT_MINUTES: int = 15
    
    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WEBSOCKET_PORT: int = 8001
    
    # LLM Providers
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    
    # Models
    REASONING_MODEL: str = "gpt-4o"
    VISION_MODEL: str = "gpt-4-vision-preview"
    
    # Logging
    LOG_LEVEL: str = "DEBUG"
    LOG_FORMAT: Literal["json", "console"] = "json"
    
    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


settings = Settings()

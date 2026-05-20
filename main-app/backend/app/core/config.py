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
        extra="ignore",
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
    WEBSOCKET_EXTERNAL_HOST: str = ""
    
    # LLM Providers
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    
    # Models
    REASONING_MODEL: str = "gpt-4o"
    VISION_MODEL: str = "gpt-4-vision-preview"
    
    # Supabase (remote auth — desktop app validates JWT against Supabase)
    SUPABASE_URL: str = ""
    SUPABASE_JWT_SECRET: str = ""
    
    # Gateway (vendor-agnostic LLM proxy)
    GATEWAY_PROVIDER: str = "direct"  # direct | zuplo | kong | aws
    GATEWAY_BASE_URL: str = ""        # e.g. https://llm.lumenatech.ai
    GATEWAY_ADMIN_KEY: str = ""       # admin key for creating consumers

    # Dropbox OAuth (training-data source)
    DROPBOX_CLIENT_ID: str = ""
    DROPBOX_CLIENT_SECRET: str = ""
    DROPBOX_REDIRECT_URI: str = ""

    # Google Drive OAuth (training-data source — separate from Google sign-in)
    GOOGLE_DRIVE_CLIENT_ID: str = ""
    GOOGLE_DRIVE_CLIENT_SECRET: str = ""
    GOOGLE_DRIVE_REDIRECT_URI: str = ""

    # Box OAuth (training-data source)
    BOX_CLIENT_ID: str = ""
    BOX_CLIENT_SECRET: str = ""
    BOX_REDIRECT_URI: str = ""

    # Logging
    LOG_LEVEL: str = "DEBUG"
    LOG_FORMAT: Literal["json", "console"] = "json"
    
    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


settings = Settings()

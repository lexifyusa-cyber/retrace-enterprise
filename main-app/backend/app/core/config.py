"""
Application configuration using Pydantic Settings.
"""

import os
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

_backend_dir = Path(__file__).parent.parent.parent  # app/core/config.py -> app -> main-app/backend


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
    DATABASE_URL: str = "postgresql+asyncpg://retrace:retrace_dev_password@localhost:5432/retrace"
    
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
    RETRACE_ENABLE_TOOL_SEARCH: bool = False

    # TeamKnowParse KB pipeline
    TEAMKNOWPARSE_KB_ENABLED: bool = False
    TEAMKNOWPARSE_VECTOR_ENABLED: bool = False
    TEAMKNOWPARSE_PIPELINE_ENABLED: bool = False
    TEAMKNOWPARSE_PROJECT_DIR: str = str((_backend_dir.parent.parent / "teamknowparse").absolute())
    TEAMKNOWPARSE_UPLOADS_DIR: str = str((_backend_dir.parent.parent / "teamknowparse" / "uploads").absolute())
    TEAMKNOWPARSE_RUNS_DIR: str = str((_backend_dir.parent.parent / "teamknowparse" / "runs").absolute())
    TEAMKNOWPARSE_COMMAND: str = ""
    TEAMKNOWPARSE_MAX_UPLOAD_MB: int = 1024
    TEAMKNOWPARSE_CHROMA_DIR: str = str((_backend_dir / "chroma_teamknowparse").absolute())
    TEAMKNOWPARSE_CHROMA_COLLECTION: str = "teamknowparse_kb_chunks"
    TEAMKNOWPARSE_EMBEDDING_MODEL: str = "text-embedding-3-small"
    TEAMKNOWPARSE_EMBEDDING_BASE_URL: str = ""
    TEAMKNOWPARSE_EMBEDDING_BATCH_SIZE: int = 96
    TEAMKNOWPARSE_EMBEDDING_CONCURRENCY: int = 4
    TEAMKNOWPARSE_EMBEDDING_SUBCHUNK_CHARS: int = 1600
    TEAMKNOWPARSE_EMBEDDING_SUBCHUNK_OVERLAP: int = 160
    TEAMKNOWPARSE_EMBEDDING_BATCH_MAX_CHARS: int = 120000
    TEAMKNOWPARSE_EMBEDDING_DEBUG_LOG: bool = False
    TEAMKNOWPARSE_VLM_ENDPOINT: str = ""
    TEAMKNOWPARSE_VLM_MODEL: str = "qwen3.5-2b"
    TEAMKNOWPARSE_VLM_WORKERS: int = 10
    TEAMKNOWPARSE_VLM_BATCH_SIZE: int = 1
    TEAMKNOWPARSE_MAX_INFLIGHT_VLM_BATCHES: int = 8
    TEAMKNOWPARSE_VLM_REQUEST_TIMEOUT_SECONDS: int = 45
    TEAMKNOWPARSE_MIXED_VLM_ENABLED: bool = True
    TEAMKNOWPARSE_MIXED_VLM_MIN_IMAGE_AREA_RATIO: float = 0.04
    TEAMKNOWPARSE_MIXED_VLM_MAX_REGIONS_PER_PAGE: int = 2
    TEAMKNOWPARSE_MIXED_VLM_HEADER_FOOTER_BAND_RATIO: float = 0.10
    TEAMKNOWPARSE_MIXED_VLM_MIN_CROP_WIDTH_PX: int = 96
    TEAMKNOWPARSE_MIXED_VLM_MIN_CROP_HEIGHT_PX: int = 96
    TEAMKNOWPARSE_VLM_TEMPERATURE: float = 0.0
    TEAMKNOWPARSE_VLM_TOP_P: float = 1.0
    TEAMKNOWPARSE_VLM_TOP_K: int = 1
    TEAMKNOWPARSE_VLM_MAX_TOKENS: int = 1024
    TEAMKNOWPARSE_VLM_COMPLEX_MAX_TOKENS: int = 2048
    TEAMKNOWPARSE_VLM_MIN_PIXELS: int = 200704
    TEAMKNOWPARSE_VLM_MAX_PIXELS: int = 1003520
    TEAMKNOWPARSE_RENDER_ZOOM: float = 1.5
    TEAMKNOWPARSE_MAX_RETRIES: int = 1
    TEAMKNOWPARSE_VLM_RETRY_BACKOFF_SECONDS: float = 0.75
    TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_KEY: str = ""
    TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_KEY_FILE: str = ""
    TEAMKNOWPARSE_ARTIFACT_ENCRYPTION_ENABLED: bool = False
    EMBEDDING_API_KEY: str = ""
    
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
    RETRACE_LLM_LOG_FALLBACK: bool = False
    LOG_FORMAT: Literal["json", "console"] = "json"
    
    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


settings = Settings()

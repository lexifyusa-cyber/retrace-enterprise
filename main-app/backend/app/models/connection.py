"""
Connection model — stored credentials for external data sources used as
training data inputs (SFTP, SMB, AWS S3, Azure Blob Storage).

Credentials live in `config_encrypted` (JSON encrypted with the chunk key).
The model is tenant-scoped so each tenant manages its own connections.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Connection(Base):
    """A reusable, encrypted credential bundle for a remote data source."""

    __tablename__ = "connections"

    connection_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    tenant_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("tenants.tenant_id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Allowed: "sftp" | "smb" | "s3" | "azure_blob"
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Encrypted JSON blob of source-type-specific config (host, key, secret, etc.)
    config_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Last test result (cached for the UI)
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_test_status: Mapped[Optional[str]] = mapped_column(
        String(20)
    )  # "ok" | "error" | None
    last_test_error: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.user_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self, include_config: bool = False) -> dict:
        """Serialise. By default config is NOT returned — secrets stay server-side."""
        d = {
            "connection_id": self.connection_id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "source_type": self.source_type,
            "last_tested_at": self.last_tested_at.isoformat() if self.last_tested_at else None,
            "last_test_status": self.last_test_status,
            "last_test_error": self.last_test_error,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_config:
            from app.services.sources import decrypt_config
            try:
                d["config"] = decrypt_config(self.config_encrypted)
                # Strip out password-like fields even when "include_config" — we
                # show metadata fields (host, port, bucket) but never the secret.
                for secret_key in ("password", "secret_key", "private_key", "account_key", "refresh_token", "access_token"):
                    d["config"].pop(secret_key, None)
            except Exception:
                d["config"] = {}
        return d

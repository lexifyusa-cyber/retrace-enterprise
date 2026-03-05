"""
POD (Agent) database model.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Pod(Base):
    """POD Agent registry model."""
    
    __tablename__ = "pods"
    
    pod_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4())
    )
    product_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        nullable=True,
    )
    pod_name: Mapped[str] = mapped_column(String(255), nullable=False)
    machine_hostname: Mapped[Optional[str]] = mapped_column(String(255))
    os_type: Mapped[Optional[str]] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(
        String(50),
        default="offline"
    )  # online, offline, degraded
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime)
    connection_url: Mapped[Optional[str]] = mapped_column(Text)
    auth_certificate: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )
    metadata_json: Mapped[Optional[dict]] = mapped_column(
        JSON,
        default=dict
    )
    
    # Relationships
    folder_groups = relationship(
        "FolderGroup",
        back_populates="pod",
        cascade="all, delete-orphan"
    )
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "pod_id": self.pod_id,
            "product_id": self.product_id,
            "pod_name": self.pod_name,
            "machine_hostname": self.machine_hostname,
            "os_type": self.os_type,
            "status": self.status,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.metadata_json or {},
        }

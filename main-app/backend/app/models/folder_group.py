"""
Folder Group and Folder Path database models.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class FolderGroup(Base):
    """Folder Group model - logical grouping of folders for training."""
    
    __tablename__ = "folder_groups"
    
    group_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4())
    )
    product_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        nullable=True  # Temporarily nullable for migration, should be False for new groups
    )
    pod_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("pods.pod_id", ondelete="CASCADE"),
        nullable=False
    )
    group_name: Mapped[str] = mapped_column(String(255), nullable=False)
    group_type: Mapped[str] = mapped_column(
        String(100),
        default="code"
    )  # code, documentation, diagrams, configuration, tickets, other
    namespace: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )
    last_trained: Mapped[Optional[datetime]] = mapped_column(DateTime)
    training_status: Mapped[str] = mapped_column(
        String(50),
        default="pending"
    )  # pending, training, completed, failed
    metadata_json: Mapped[Optional[dict]] = mapped_column(
        JSON,
        default=dict
    )
    
    # Relationships
    product = relationship("Product", back_populates="folder_groups")
    pod = relationship("Pod", back_populates="folder_groups")
    folder_paths = relationship(
        "FolderPath",
        back_populates="folder_group",
        cascade="all, delete-orphan"
    )
    training_jobs = relationship(
        "TrainingJob",
        back_populates="folder_group",
        cascade="all, delete-orphan"
    )
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        # Normalize removed types so UI and clients never see diagrams/configuration
        gtype = self.group_type
        if gtype == "diagrams":
            gtype = "documentation"
        elif gtype == "configuration":
            gtype = "code"
        return {
            "group_id": self.group_id,
            "product_id": self.product_id,
            "pod_id": self.pod_id,
            "group_name": self.group_name,
            "group_type": gtype,
            "namespace": self.namespace,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_trained": self.last_trained.isoformat() if self.last_trained else None,
            "training_status": self.training_status,
            "folder_paths": [fp.to_dict() for fp in self.folder_paths] if self.folder_paths else [],
            "metadata": self.metadata_json or {},
        }


class FolderPath(Base):
    """Folder Path model - individual paths within a folder group."""
    
    __tablename__ = "folder_paths"
    
    path_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4())
    )
    group_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("folder_groups.group_id", ondelete="CASCADE"),
        nullable=False
    )
    absolute_path: Mapped[str] = mapped_column(Text, nullable=False)
    scan_recursive: Mapped[bool] = mapped_column(Boolean, default=True)
    file_filters: Mapped[Optional[dict]] = mapped_column(
        JSON,
        default=lambda: {"include": [], "exclude": []}
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )
    
    # Relationships
    folder_group = relationship("FolderGroup", back_populates="folder_paths")
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "path_id": self.path_id,
            "group_id": self.group_id,
            "absolute_path": self.absolute_path,
            "scan_recursive": self.scan_recursive,
            "file_filters": self.file_filters or {"include": [], "exclude": []},
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

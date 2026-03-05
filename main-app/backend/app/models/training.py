"""
Training Job database model.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, ForeignKey, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class TrainingJob(Base):
    """Training Job model - tracks training progress."""
    
    __tablename__ = "training_jobs"
    
    job_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4())
    )
    group_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("folder_groups.group_id", ondelete="CASCADE"),
        nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(50),
        default="queued"
    )  # queued, running, completed, failed, cancelled
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    progress_data: Mapped[Optional[dict]] = mapped_column(
        JSON,
        default=dict
    )
    statistics: Mapped[Optional[dict]] = mapped_column(
        JSON,
        default=dict
    )
    error_log: Mapped[Optional[str]] = mapped_column(Text)  # Store as JSON string
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )
    
    # Relationships
    folder_group = relationship("FolderGroup", back_populates="training_jobs")
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "job_id": self.job_id,
            "group_id": self.group_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "progress_data": self.progress_data or {},
            "statistics": self.statistics or {},
            "error_log": self.error_log,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

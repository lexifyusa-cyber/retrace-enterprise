"""
Product database model.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Product(Base):
    """Product model - contains multiple folder groups."""
    
    __tablename__ = "products"
    
    product_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4())
    )
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    auto_generate_description: Mapped[bool] = mapped_column(default=True)
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
        back_populates="product",
        cascade="all, delete-orphan"
    )
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "product_id": self.product_id,
            "tenant_id": self.tenant_id,
            "created_by": self.created_by,
            "product_name": self.product_name,
            "description": self.description,
            "auto_generate_description": self.auto_generate_description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "folder_groups": [fg.to_dict() for fg in self.folder_groups] if self.folder_groups else [],
            "metadata": self.metadata_json or {},
        }

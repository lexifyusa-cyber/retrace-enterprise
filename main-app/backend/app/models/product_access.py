"""
Product access control — grants individual users access to specific products.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ProductAccess(Base):
    __tablename__ = "product_access"

    access_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    product_id: Mapped[str] = mapped_column(String(36), ForeignKey("products.product_id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    permission: Mapped[str] = mapped_column(String(20), default="view")
    # view | query | edit | admin
    granted_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "access_id": self.access_id,
            "product_id": self.product_id,
            "user_id": self.user_id,
            "permission": self.permission,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at.isoformat() if self.granted_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

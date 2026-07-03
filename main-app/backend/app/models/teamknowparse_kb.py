"""
TeamKnowParse knowledge-base tables.

These tables live beside the original encrypted ReTrace KB blob. They are used
only by the TeamKnowParse pipeline and keep SQLite as the source of truth while
Chroma remains a rebuildable vector index.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class TeamKnowParseKbFolder(Base):
    __tablename__ = "teamknowparse_kb_folders"

    folder_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    team_id: Mapped[str] = mapped_column(String(36), ForeignKey("teams.team_id", ondelete="CASCADE"), index=True)
    folder_path: Mapped[str] = mapped_column(Text, nullable=False)
    folder_name: Mapped[Optional[str]] = mapped_column(String(255))
    folder_description: Mapped[Optional[str]] = mapped_column(Text)
    source_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TeamKnowParseKbFile(Base):
    __tablename__ = "teamknowparse_kb_files"

    file_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    team_id: Mapped[str] = mapped_column(String(36), ForeignKey("teams.team_id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("teamknowparse_kb_folders.folder_id", ondelete="SET NULL"))
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    file_description: Mapped[Optional[str]] = mapped_column(Text)
    source_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    folder = relationship("TeamKnowParseKbFolder")


class TeamKnowParseKbChunk(Base):
    __tablename__ = "teamknowparse_kb_chunks"
    __table_args__ = (
        UniqueConstraint("team_id", "source_run_id", "chunk_index", name="uq_teamknowparse_kb_chunks_team_run_index"),
    )

    chunk_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    team_id: Mapped[str] = mapped_column(String(36), ForeignKey("teams.team_id", ondelete="CASCADE"), index=True)
    file_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("teamknowparse_kb_files.file_id", ondelete="SET NULL"), index=True)
    source_run_id: Mapped[str] = mapped_column(String(128), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[Optional[str]] = mapped_column(String(512))
    page_number: Mapped[Optional[int]] = mapped_column(Integer)
    page_type: Mapped[Optional[str]] = mapped_column(String(64))
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    embedding_status: Mapped[str] = mapped_column(String(32), default="pending")
    embedding_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    file = relationship("TeamKnowParseKbFile")


class TeamKnowParseKbIngestionRun(Base):
    __tablename__ = "teamknowparse_kb_ingestion_runs"

    ingestion_run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    team_id: Mapped[str] = mapped_column(String(36), ForeignKey("teams.team_id", ondelete="CASCADE"), index=True)
    training_job_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("training_jobs.job_id", ondelete="SET NULL"))
    teamknowparse_run_id: Mapped[str] = mapped_column(String(128), index=True)
    run_dir: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    metrics_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    validation_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    error_log: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

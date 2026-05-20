"""
Data models for the revamped 4-phase RAG training pipeline.

Phase 1 — Build file/folder tree + extension-based exclusion
Phase 2 — LLM project analysis + keep/exclude filtering
Phase 3 — Text extraction (direct + OCR for mixed files)
Phase 4 — Build compressed+encrypted KB with LLM analysis stored inside

Backward-compatible SQLAlchemy models (ChunkRecord, KnowledgeBase) are
retained so existing databases keep working.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import String, Integer, DateTime, Text, JSON, LargeBinary, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ContentType(str, Enum):
    """LLM-assigned content type for a file (Phase 2)."""
    TEXT = "text"
    IMAGE = "image"
    TEXT_OR_IMAGE = "text_or_image"
    EXCLUDED = "excluded"


class ProcessingType(str, Enum):
    """Chunk type in ChromaDB — distinguishes content vs analysis chunks."""
    CONTENT = "content"
    FILE_ANALYSIS = "file_analysis"
    FOLDER_ANALYSIS = "folder_analysis"
    PROJECT_ANALYSIS = "project_analysis"
    # Legacy values kept for backward compat with existing chunks
    CODE = "code"
    DOC = "doc"
    TICKET_EXPORT = "ticket_export"
    DOC_WITH_DIAGRAMS = "doc_with_diagrams"
    DIAGRAM_IMAGE = "diagram_image"
    SUMMARY = "summary"
    EXPERT_QA = "expert_qa"
    FOLDER_STRUCTURE = "folder_structure"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Tree node — the central data structure flowing through all 4 phases
# ---------------------------------------------------------------------------

class TreeNode(BaseModel):
    """A file or folder in the training tree.

    Tags accumulate across phases:
      Phase 1 → phase1_excluded (bool)
      Phase 2 → decision, content_type, description, component, …
      Phase 3 → extracted_text, extraction_method
    """
    name: str
    path: str
    is_file: bool
    ext: str = ""
    size_bytes: int = 0
    modified_at: str = ""
    children: list[TreeNode] = Field(default_factory=list)

    # Phase 1 tags
    phase1_excluded: bool = False
    phase1_reason: str = ""

    # Phase 2 tags (LLM analysis)
    decision: str = ""                 # "keep" | "excluded" | ""
    content_type: str = ""             # ContentType value or ""
    description: str = ""              # LLM's description of this file/folder
    component: str = ""                # Which component/module this belongs to
    role: str = ""                     # The role this file/folder plays
    technologies: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)
    importance: str = ""               # "critical" | "high" | "medium" | "low"
    phase2_reason: str = ""            # Why excluded (if excluded in phase 2)

    # Phase 3 tags (text extraction)
    extracted_text: str = ""
    extraction_method: str = ""        # "direct" | "ocr" | "failed" | ""
    extraction_chars: int = 0

    model_config = {"arbitrary_types_allowed": True}

    def file_count(self, *, include_excluded: bool = True) -> int:
        """Count files in this subtree."""
        if self.is_file:
            if include_excluded:
                return 1
            return 0 if (self.phase1_excluded or self.decision == "excluded") else 1
        return sum(c.file_count(include_excluded=include_excluded) for c in self.children)

    def folder_count(self) -> int:
        if self.is_file:
            return 0
        return 1 + sum(c.folder_count() for c in self.children)

    def kept_files(self) -> list[TreeNode]:
        """Return all file nodes with decision == 'keep'."""
        if self.is_file:
            return [self] if self.decision == "keep" else []
        result: list[TreeNode] = []
        for c in self.children:
            result.extend(c.kept_files())
        return result

    def all_files(self) -> list[TreeNode]:
        """Return all file nodes in the subtree."""
        if self.is_file:
            return [self]
        result: list[TreeNode] = []
        for c in self.children:
            result.extend(c.all_files())
        return result

    def all_folders(self) -> list[TreeNode]:
        """Return all folder nodes in the subtree (including self)."""
        if self.is_file:
            return []
        result: list[TreeNode] = [self]
        for c in self.children:
            result.extend(c.all_folders())
        return result


# ---------------------------------------------------------------------------
# Project analysis — the LLM's understanding of the project (Phase 2)
# ---------------------------------------------------------------------------

class ComponentInfo(BaseModel):
    name: str = ""
    description: str = ""
    primary_folders: list[str] = Field(default_factory=list)


class ProjectAnalysis(BaseModel):
    """Top-level LLM understanding of the entire project."""
    summary: str = ""
    technologies: list[str] = Field(default_factory=list)
    architecture_style: str = ""
    key_components: list[ComponentInfo] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pydantic transfer models (kept for backward compat + new pipeline)
# ---------------------------------------------------------------------------

class FileRecord(BaseModel):
    """A file discovered by the crawler (legacy compat)."""
    path: str
    name: str
    ext: str
    size_bytes: int
    modified_at: str


class CategorizedFile(FileRecord):
    """Legacy: a file with its content category."""
    content_category: str = "text"
    ocr_required: bool = False
    estimated_text_length: int = 0


class ClassifiedFile(FileRecord):
    """Legacy: a file with its assigned processing type and sub-category."""
    processing_type: ProcessingType = ProcessingType.OTHER
    sub_category: str = "general"
    embed: bool = True


class ChunkMetadata(BaseModel):
    """Metadata attached to a chunk."""
    language: Optional[str] = None
    symbol_name: Optional[str] = None
    content_category: Optional[str] = None
    source_file: Optional[str] = None
    chunk_index: Optional[int] = None
    score: Optional[float] = None
    chunk_type: Optional[str] = None   # "content" | "file_analysis" | "folder_analysis" | "project_analysis"
    component: Optional[str] = None
    description: Optional[str] = None
    # Backward-compat fields used by legacy code and QA sync
    sub_category: Optional[str] = None
    file_description: Optional[str] = None
    summary_scope: Optional[str] = None
    page_number: Optional[int] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    feature: Optional[str] = None
    concepts: Optional[str] = None


class Chunk(BaseModel):
    """A processed text chunk ready for embedding."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    source_path: str
    processing_type: ProcessingType = ProcessingType.CONTENT
    text: str
    metadata: ChunkMetadata = Field(default_factory=ChunkMetadata)


# ---------------------------------------------------------------------------
# SQLAlchemy persistent models
# ---------------------------------------------------------------------------

class ChunkRecord(Base):
    """Legacy persisted chunk — kept for backward compat / migration."""

    __tablename__ = "chunk_records"

    chunk_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    team_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    processing_type: Mapped[str] = mapped_column(String(50), nullable=False)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_chunk_team_type", "team_id", "processing_type"),
    )

    def to_chunk(self) -> Chunk:
        return Chunk(
            id=self.chunk_id,
            source_path=self.source_path,
            processing_type=ProcessingType(self.processing_type),
            text=self.text or "",
            metadata=ChunkMetadata(**(self.metadata_json or {})),
        )


class KnowledgeBase(Base):
    """Compressed + encrypted knowledge base blob per team.

    Stores the full extracted text AND the LLM project analysis as a
    single gzip-compressed, AES-256-GCM encrypted blob.
    """

    __tablename__ = "knowledge_bases"

    kb_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    team_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        unique=True,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    compressed_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    total_text_bytes: Mapped[int] = mapped_column(Integer, default=0)
    compressed_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )

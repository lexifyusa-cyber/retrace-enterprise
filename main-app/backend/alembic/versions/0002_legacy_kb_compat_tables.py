"""legacy KB compatibility tables

Revision ID: 0002_legacy_kb_compat_tables
Revises: 0001_initial_fastapi_schema
Create Date: 2026-07-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_legacy_kb_compat_tables"
down_revision: Union[str, None] = "0001_initial_fastapi_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kb_folders",
        sa.Column("folder_id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("folder_path", sa.Text(), nullable=False),
        sa.Column("folder_name", sa.String(255)),
        sa.Column("folder_description", sa.Text()),
        sa.Column("source_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_kb_folders_team_id", "kb_folders", ["team_id"])

    op.create_table(
        "kb_files",
        sa.Column("file_id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("folder_id", sa.String(36), sa.ForeignKey("kb_folders.folder_id", ondelete="SET NULL")),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.Column("file_description", sa.Text()),
        sa.Column("source_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_kb_files_team_id", "kb_files", ["team_id"])

    op.create_table(
        "kb_chunks",
        sa.Column("chunk_id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.String(36), sa.ForeignKey("kb_files.file_id", ondelete="SET NULL")),
        sa.Column("source_run_id", sa.String(128), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(512)),
        sa.Column("page_number", sa.Integer()),
        sa.Column("page_type", sa.String(64)),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON()),
        sa.Column("embedding_status", sa.String(32), nullable=False),
        sa.Column("embedding_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("team_id", "source_run_id", "chunk_index", name="uq_kb_chunks_team_run_index"),
    )
    op.create_index("ix_kb_chunks_team_id", "kb_chunks", ["team_id"])
    op.create_index("ix_kb_chunks_file_id", "kb_chunks", ["file_id"])
    op.create_index("ix_kb_chunks_source_run_id", "kb_chunks", ["source_run_id"])

    op.create_table(
        "kb_ingestion_runs",
        sa.Column("ingestion_run_id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("training_job_id", sa.String(36), sa.ForeignKey("training_jobs.job_id", ondelete="SET NULL")),
        sa.Column("teamknowparse_run_id", sa.String(128), nullable=False),
        sa.Column("run_dir", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("metrics_json", sa.JSON()),
        sa.Column("validation_json", sa.JSON()),
        sa.Column("error_log", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_kb_ingestion_runs_team_id", "kb_ingestion_runs", ["team_id"])
    op.create_index("ix_kb_ingestion_runs_teamknowparse_run_id", "kb_ingestion_runs", ["teamknowparse_run_id"])


def downgrade() -> None:
    op.drop_table("kb_ingestion_runs")
    op.drop_table("kb_chunks")
    op.drop_table("kb_files")
    op.drop_table("kb_folders")

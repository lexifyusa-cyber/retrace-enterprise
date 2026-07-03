"""add embedding settings

Revision ID: 0003_add_embedding_settings
Revises: 0002_legacy_kb_compat_tables
Create Date: 2026-07-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_add_embedding_settings"
down_revision: Union[str, None] = "0002_legacy_kb_compat_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = [
    ("embedding_model", sa.Column("embedding_model", sa.String(length=255), nullable=True)),
    ("embedding_base_url", sa.Column("embedding_base_url", sa.Text(), nullable=True)),
    ("embedding_api_key", sa.Column("embedding_api_key", sa.Text(), nullable=True)),
    ("embedding_chroma_collection", sa.Column("embedding_chroma_collection", sa.String(length=255), nullable=True)),
    ("embedding_debug_log", sa.Column("embedding_debug_log", sa.Boolean(), nullable=True)),
]


def _existing_columns() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns("llm_settings")}


def upgrade() -> None:
    existing = _existing_columns()
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column("llm_settings", column)


def downgrade() -> None:
    existing = _existing_columns()
    for name, _column in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("llm_settings", name)

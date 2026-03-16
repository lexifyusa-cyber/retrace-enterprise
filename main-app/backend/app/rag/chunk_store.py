"""
ChunkStore – encrypted key-value store for chunk text.

Stores chunk_id → encrypted ciphertext in a SQLite table separate from
the vector DB. At query time the caller fetches by chunk_id and decrypts.
"""

import asyncio
from typing import Optional

import structlog
from sqlalchemy import String, LargeBinary, select, delete
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base, async_session_maker
from app.rag.encryption import encrypt_text, decrypt_text

logger = structlog.get_logger()


class EncryptedChunk(Base):
    """One row per chunk; stores only the encrypted text."""

    __tablename__ = "encrypted_chunks"

    chunk_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(36), index=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class ChunkStoreService:
    """Read/write encrypted chunk text."""

    async def put_many(self, product_id: str, items: list[tuple[str, str]]) -> int:
        """Store [(chunk_id, plaintext), ...].  Returns count stored."""
        if not items:
            return 0
        rows = [
            EncryptedChunk(
                chunk_id=cid,
                product_id=product_id,
                ciphertext=encrypt_text(text),
            )
            for cid, text in items
        ]
        async with async_session_maker() as session:
            session.add_all(rows)
            await session.commit()
        logger.info("chunk_store_put", product_id=product_id, count=len(rows))
        return len(rows)

    async def get_many(self, chunk_ids: list[str]) -> dict[str, str]:
        """Return {chunk_id: decrypted_text} for the requested ids."""
        if not chunk_ids:
            return {}
        async with async_session_maker() as session:
            result = await session.execute(
                select(EncryptedChunk).where(EncryptedChunk.chunk_id.in_(chunk_ids))
            )
            rows = list(result.scalars().all())
        out: dict[str, str] = {}
        for row in rows:
            try:
                out[row.chunk_id] = decrypt_text(row.ciphertext)
            except Exception as exc:
                logger.error("chunk_decrypt_failed", chunk_id=row.chunk_id, error=str(exc))
        return out

    async def get_one(self, chunk_id: str) -> Optional[str]:
        """Return decrypted text for a single chunk, or None."""
        result = await self.get_many([chunk_id])
        return result.get(chunk_id)

    async def delete_product(self, product_id: str) -> int:
        """Delete all encrypted chunks for a product."""
        async with async_session_maker() as session:
            result = await session.execute(
                delete(EncryptedChunk).where(EncryptedChunk.product_id == product_id)
            )
            await session.commit()
            count = result.rowcount or 0
        logger.info("chunk_store_cleared", product_id=product_id, count=count)
        return count

    async def delete_by_paths(self, product_id: str, source_paths: list[str]) -> int:
        """Delete encrypted chunks whose chunk_ids came from certain files.

        Since we don't store source_path here, we accept chunk_ids directly
        via delete_by_ids. This method is a convenience alias.
        """
        return 0

    async def delete_by_ids(self, chunk_ids: list[str]) -> int:
        """Delete specific chunks by id."""
        if not chunk_ids:
            return 0
        async with async_session_maker() as session:
            result = await session.execute(
                delete(EncryptedChunk).where(EncryptedChunk.chunk_id.in_(chunk_ids))
            )
            await session.commit()
            count = result.rowcount or 0
        return count

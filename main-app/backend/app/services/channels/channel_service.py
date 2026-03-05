"""
Channel service — orchestrates reading from Slack/Teams channels,
ingesting history into the product RAG knowledge base, and
auto-responding to questions in channels.
"""

import json
import re
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel_connection import ChannelConnection
from app.services.channels.slack_bot import SlackBotAdapter, SlackMessage
from app.services.channels.teams_bot import TeamsBotAdapter, TeamsMessage
from app.services.query_service import query_service

logger = structlog.get_logger()

# Heuristic: messages that look like questions
_QUESTION_RE = re.compile(
    r"(\?\s*$|^(how|what|why|where|when|who|which|can|could|should|is|are|does|do|will)\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _looks_like_question(text: str) -> bool:
    """Return True if the text looks like a question worth answering."""
    return bool(_QUESTION_RE.search(text.strip()))


def _messages_to_training_text(
    messages: list,
    channel_name: str,
) -> str:
    """Convert a list of channel messages into a single text document
    suitable for ingestion into the RAG pipeline."""
    lines = [f"# Channel conversation: {channel_name}\n"]
    for m in messages:
        if isinstance(m, SlackMessage):
            lines.append(f"[User {m.user}]: {m.text}")
        elif isinstance(m, TeamsMessage):
            lines.append(f"[{m.user}]: {m.text}")
        else:
            lines.append(str(m))
    return "\n".join(lines)


class ChannelService:
    """Orchestrate channel read, ingest, and respond operations."""

    # ── Adapter construction ──────────────────────────────────────────

    def _get_slack_adapter(self, conn: ChannelConnection) -> SlackBotAdapter:
        token = conn.bot_token_encrypted or ""
        return SlackBotAdapter(bot_token=token)

    def _get_teams_adapter(self, conn: ChannelConnection) -> TeamsBotAdapter:
        creds = json.loads(conn.bot_token_encrypted or "{}")
        return TeamsBotAdapter(
            client_id=creds.get("client_id", ""),
            client_secret=creds.get("client_secret", ""),
            tenant_id=creds.get("tenant_id", ""),
        )

    # ── Test ──────────────────────────────────────────────────────────

    async def test_connection(self, conn: ChannelConnection) -> dict:
        """Verify that the credentials work and the channel is accessible."""
        try:
            if conn.platform == "slack":
                adapter = self._get_slack_adapter(conn)
                info = await adapter.test_auth()
                messages = await adapter.fetch_history(conn.channel_id, limit=3)
                return {
                    "success": True,
                    "message": f"Connected as {info.get('user', '?')}. "
                    f"Found {len(messages)} recent messages in #{conn.channel_name}.",
                }
            elif conn.platform == "teams":
                adapter = self._get_teams_adapter(conn)
                await adapter.test_auth()
                messages = await adapter.fetch_history(
                    conn.team_id or "", conn.channel_id, limit=3
                )
                return {
                    "success": True,
                    "message": f"Connected. Found {len(messages)} recent messages "
                    f"in {conn.channel_name}.",
                }
            else:
                return {"success": False, "message": f"Unknown platform: {conn.platform}"}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    # ── Preview ───────────────────────────────────────────────────────

    async def preview_messages(
        self, conn: ChannelConnection, limit: int = 20
    ) -> list[dict]:
        """Fetch recent messages for preview (without ingesting)."""
        if conn.platform == "slack":
            adapter = self._get_slack_adapter(conn)
            messages = await adapter.fetch_history(conn.channel_id, limit=limit)
            return [
                {"user": m.user, "text": m.text, "ts": m.ts} for m in messages
            ]
        elif conn.platform == "teams":
            adapter = self._get_teams_adapter(conn)
            messages = await adapter.fetch_history(
                conn.team_id or "", conn.channel_id, limit=limit
            )
            return [
                {
                    "user": m.user,
                    "text": m.text,
                    "ts": m.created.isoformat(),
                }
                for m in messages
            ]
        return []

    # ── Ingest history into RAG ───────────────────────────────────────

    async def sync_history(
        self, conn: ChannelConnection, session: AsyncSession
    ) -> dict:
        """Fetch messages since last sync and ingest into the product's
        knowledge base as training data."""
        since = conn.last_synced_at

        if conn.platform == "slack":
            adapter = self._get_slack_adapter(conn)
            messages = await adapter.fetch_history(
                conn.channel_id, since=since, limit=200
            )
        elif conn.platform == "teams":
            adapter = self._get_teams_adapter(conn)
            messages = await adapter.fetch_history(
                conn.team_id or "", conn.channel_id, since=since, limit=200
            )
        else:
            return {"synced": 0, "error": f"Unknown platform: {conn.platform}"}

        if not messages:
            return {"synced": 0, "message": "No new messages since last sync."}

        # Build training text
        text = _messages_to_training_text(messages, conn.channel_name)

        # Ingest via the RAG embedding pipeline
        try:
            from app.rag.embedding_index import EmbeddingIndexService
            from app.rag.clients import OpenAIEmbeddingClient
            from app.api.settings import get_active_llm_settings

            llm = await get_active_llm_settings(session)
            emb_key = llm.get("embeddings_api_key") or llm.get("api_key")
            emb_url = llm.get("embeddings_api_url") or llm.get("api_url") or None
            emb_model = llm.get("embeddings_model") or "text-embedding-3-small"

            if emb_key:
                embedding_client = OpenAIEmbeddingClient(
                    api_key=emb_key, base_url=emb_url, model=emb_model
                )
                index = EmbeddingIndexService(embedding_client=embedding_client)

                chunk_size = 1500
                chunks = [
                    text[i : i + chunk_size] for i in range(0, len(text), chunk_size)
                ]
                namespace = f"channel_{conn.connection_id}"
                for i, chunk in enumerate(chunks):
                    await index.upsert(
                        doc_id=f"{namespace}_{i}",
                        text=chunk,
                        metadata={
                            "product_id": conn.product_id,
                            "source": "channel",
                            "channel_name": conn.channel_name,
                            "platform": conn.platform,
                            "namespace": namespace,
                        },
                        namespace=namespace,
                    )
                logger.info(
                    "channel_history_ingested",
                    connection_id=conn.connection_id,
                    chunks=len(chunks),
                    messages=len(messages),
                )
        except Exception as exc:
            logger.error("channel_ingest_failed", error=str(exc))
            return {"synced": len(messages), "error": str(exc)}

        # Update last_synced_at
        conn.last_synced_at = datetime.utcnow()
        await session.flush()

        return {"synced": len(messages), "chunks": len(chunks), "message": "Sync complete."}

    # ── Monitor & respond ─────────────────────────────────────────────

    async def check_and_respond(
        self, conn: ChannelConnection, session: AsyncSession
    ) -> dict:
        """Fetch new messages, find questions, and post AI answers."""
        if not conn.auto_respond:
            return {"responded": 0, "reason": "auto_respond is disabled"}

        since = conn.last_synced_at or datetime.utcnow()

        if conn.platform == "slack":
            adapter = self._get_slack_adapter(conn)
            messages = await adapter.fetch_history(
                conn.channel_id, since=since, limit=50
            )
            questions = [m for m in messages if _looks_like_question(m.text)]

            responded = 0
            for q in questions:
                try:
                    result = await query_service.process_query(
                        question=q.text,
                        product_ids=[conn.product_id],
                        session=session,
                    )
                    answer = result.get("answer", "")
                    if answer and result.get("confidence_score", 0) > 0.3:
                        await adapter.post_message(
                            conn.channel_id, answer, thread_ts=q.ts
                        )
                        responded += 1
                except Exception as exc:
                    logger.error("channel_respond_failed", error=str(exc))

            conn.last_synced_at = datetime.utcnow()
            await session.flush()
            return {"responded": responded, "questions_found": len(questions)}

        elif conn.platform == "teams":
            adapter = self._get_teams_adapter(conn)
            messages = await adapter.fetch_history(
                conn.team_id or "", conn.channel_id, since=since, limit=50
            )
            questions = [m for m in messages if _looks_like_question(m.text)]

            responded = 0
            for q in questions:
                try:
                    result = await query_service.process_query(
                        question=q.text,
                        product_ids=[conn.product_id],
                        session=session,
                    )
                    answer = result.get("answer", "")
                    if answer and result.get("confidence_score", 0) > 0.3:
                        await adapter.post_message(
                            conn.team_id or "", conn.channel_id, answer
                        )
                        responded += 1
                except Exception as exc:
                    logger.error("channel_respond_failed", error=str(exc))

            conn.last_synced_at = datetime.utcnow()
            await session.flush()
            return {"responded": responded, "questions_found": len(questions)}

        return {"responded": 0}


channel_service = ChannelService()

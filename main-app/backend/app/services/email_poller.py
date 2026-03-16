"""
Background email poller — checks all active email connections every 60 seconds
for new unread messages and processes them into drafts.
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional
import re

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_connection import EmailConnection
from app.models.email_inbox import EmailInbox
from app.models.email_message import EmailMessage
from app.services.email.factory import get_provider
from app.services.email.processor import email_processor

logger = structlog.get_logger()

_EMAIL_ANGLE_RE = re.compile(r"<([^>]+)>")

POLL_INTERVAL_SECONDS = 60
FETCH_WINDOW_MINUTES = 5


def _normalize_email(addr: str) -> str:
    if not addr:
        return ""
    addr = addr.replace("&lt;", "<").replace("&gt;", ">").strip().lower()
    m = _EMAIL_ANGLE_RE.search(addr)
    return m.group(1).strip().lower() if m else addr


class EmailPoller:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("email_poller_started", interval=POLL_INTERVAL_SECONDS)

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("email_poller_stopped")

    async def _loop(self):
        while self._running:
            try:
                await self._poll_all()
            except Exception as exc:
                logger.error("email_poller_error", error=str(exc))
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _poll_all(self):
        from app.db.database import async_session_maker

        async with async_session_maker() as session:
            result = await session.execute(
                select(EmailConnection).where(EmailConnection.status == "active")
            )
            connections = result.scalars().all()

        for conn in connections:
            try:
                synced = await self._sync_connection(conn)
                if synced > 0:
                    logger.info("email_poller_synced", connection_id=conn.connection_id, synced=synced)
            except Exception as exc:
                logger.warning("email_poller_conn_error", connection_id=conn.connection_id, error=str(exc))

    async def _sync_connection(self, conn: EmailConnection) -> int:
        from app.db.database import async_session_maker

        async with async_session_maker() as session:
            result = await session.execute(
                select(EmailInbox).where(
                    EmailInbox.connection_id == conn.connection_id,
                    EmailInbox.is_active == True,
                )
            )
            inbox_list = result.scalars().all()
            if not inbox_list:
                return 0

            inbox_emails = {i.email_address.lower().strip() for i in inbox_list}
            inbox_ids = [i.inbox_id for i in inbox_list]
            inbox_by_email = {i.email_address.lower().strip(): i for i in inbox_list}
            default_inbox = inbox_list[0]

            token_data = json.loads(conn.oauth_token_encrypted or "{}")
            client_id = getattr(conn, "client_id", None) or ""
            client_secret = getattr(conn, "client_secret", None) or ""
            provider = get_provider(
                conn,
                client_id=client_id,
                client_secret=client_secret,
                tenant_id="common",
            )
            access_token = token_data.get("access_token", "")
            since = datetime.utcnow() - timedelta(minutes=FETCH_WINDOW_MINUTES)

            try:
                messages = await provider.fetch_new_messages(
                    access_token=access_token,
                    since=since,
                )
            except Exception as exc:
                err_str = str(exc)
                is_401 = "401" in err_str
                refresh_token = token_data.get("refresh_token", "")
                if is_401 and refresh_token and client_id and client_secret and conn.provider == "zoho":
                    from app.services.email.zoho import ZohoProvider
                    zoho = ZohoProvider(client_id=client_id, client_secret=client_secret)
                    new_tokens = await zoho.refresh_token(refresh_token)
                    token_data["access_token"] = new_tokens["access_token"]
                    if new_tokens.get("refresh_token"):
                        token_data["refresh_token"] = new_tokens["refresh_token"]
                    # Persist refreshed token
                    db_conn = await session.get(EmailConnection, conn.connection_id)
                    if db_conn:
                        db_conn.oauth_token_encrypted = json.dumps(token_data)
                        await session.flush()
                    access_token = token_data["access_token"]
                    messages = await provider.fetch_new_messages(
                        access_token=access_token,
                        since=since,
                    )
                else:
                    raise

            processed = 0
            for email in messages:
                to_addr = _normalize_email(email.to_address or "")
                if to_addr and to_addr not in inbox_emails:
                    continue
                matched_inbox = inbox_by_email.get(to_addr, default_inbox) if to_addr else default_inbox
                if not matched_inbox:
                    continue
                existing = await session.execute(
                    select(EmailMessage).where(
                        EmailMessage.provider_message_id == email.provider_message_id,
                        EmailMessage.inbox_id.in_(inbox_ids),
                    )
                )
                if existing.scalars().first() is not None:
                    continue
                try:
                    msg = await email_processor.process(email, session, inbox_override=matched_inbox)
                    if msg:
                        processed += 1
                except Exception as exc:
                    logger.warning("email_poller_process_error", error=str(exc), subject=email.subject)

            await session.commit()
            return processed


email_poller = EmailPoller()

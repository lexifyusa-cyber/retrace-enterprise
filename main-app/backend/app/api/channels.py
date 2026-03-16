"""
Channels API — Slack / Teams channel connections for reading and responding.
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.db.database import get_session
from app.models.channel_connection import ChannelConnection
from app.services.channels.channel_service import channel_service
from app.core.security import require_role, CurrentUser

logger = structlog.get_logger()
router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────


class ChannelConnectionCreate(BaseModel):
    product_id: str
    platform: str = Field(..., pattern="^(slack|teams)$")
    channel_name: str = Field(..., min_length=1, max_length=255)
    channel_id: str = Field(..., min_length=1)
    # Slack: plain bot token string. Teams: JSON with client_id, client_secret, tenant_id
    bot_token: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    auto_respond: bool = False
    ingest_history: bool = True


class ChannelConnectionUpdate(BaseModel):
    is_active: Optional[bool] = None
    auto_respond: Optional[bool] = None
    ingest_history: Optional[bool] = None


class ChannelConnectionResponse(BaseModel):
    connection_id: str
    product_id: str
    platform: str
    channel_name: str
    channel_id: str
    team_id: Optional[str] = None
    is_active: bool
    auto_respond: bool
    ingest_history: bool
    last_synced_at: Optional[str] = None
    created_at: Optional[str] = None


# ── CRUD ──────────────────────────────────────────────────────────────


@router.get("/connections", response_model=list[ChannelConnectionResponse])
async def list_connections(
    product_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(require_role("admin")),
):
    query = select(ChannelConnection)
    if current_user.tenant_id:
        query = query.where(ChannelConnection.tenant_id == current_user.tenant_id)
    if product_id:
        query = query.where(ChannelConnection.product_id == product_id)
    result = await session.execute(query)
    return [
        ChannelConnectionResponse(**c.to_dict()) for c in result.scalars().all()
    ]


@router.post("/connections", response_model=ChannelConnectionResponse, status_code=201)
async def create_connection(
    data: ChannelConnectionCreate,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(require_role("admin")),
):
    conn = ChannelConnection(
        tenant_id=current_user.tenant_id,
        product_id=data.product_id,
        platform=data.platform,
        channel_name=data.channel_name,
        channel_id=data.channel_id,
        bot_token_encrypted=data.bot_token,
        team_id=data.team_id,
        auto_respond=data.auto_respond,
        ingest_history=data.ingest_history,
        is_active=True,
    )

    # Validate credentials
    test_result = await channel_service.test_connection(conn)
    if not test_result.get("success"):
        raise HTTPException(400, f"Connection test failed: {test_result.get('message')}")

    session.add(conn)
    await session.flush()
    return ChannelConnectionResponse(**conn.to_dict())


@router.patch("/{connection_id}", response_model=ChannelConnectionResponse)
async def update_connection(
    connection_id: str,
    data: ChannelConnectionUpdate,
    session: AsyncSession = Depends(get_session),
):
    conn = await session.get(ChannelConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Channel connection not found")
    if data.is_active is not None:
        conn.is_active = data.is_active
    if data.auto_respond is not None:
        conn.auto_respond = data.auto_respond
    if data.ingest_history is not None:
        conn.ingest_history = data.ingest_history
    await session.flush()
    return ChannelConnectionResponse(**conn.to_dict())


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str, session: AsyncSession = Depends(get_session)
):
    conn = await session.get(ChannelConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Channel connection not found")
    await session.delete(conn)
    await session.flush()


# ── Test / Sync / Preview ─────────────────────────────────────────────


@router.post("/{connection_id}/test")
async def test_connection(
    connection_id: str, session: AsyncSession = Depends(get_session)
):
    conn = await session.get(ChannelConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Channel connection not found")
    return await channel_service.test_connection(conn)


@router.post("/{connection_id}/sync")
async def sync_history(
    connection_id: str, session: AsyncSession = Depends(get_session)
):
    conn = await session.get(ChannelConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Channel connection not found")
    if not conn.is_active:
        raise HTTPException(400, "Connection is inactive")
    return await channel_service.sync_history(conn, session)


@router.get("/{connection_id}/preview")
async def preview_messages(
    connection_id: str,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
):
    conn = await session.get(ChannelConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Channel connection not found")
    messages = await channel_service.preview_messages(conn, limit=limit)
    return {"messages": messages, "count": len(messages)}


@router.post("/{connection_id}/respond")
async def trigger_respond(
    connection_id: str, session: AsyncSession = Depends(get_session)
):
    """Manually trigger the check-and-respond cycle."""
    conn = await session.get(ChannelConnection, connection_id)
    if not conn:
        raise HTTPException(404, "Channel connection not found")
    if not conn.is_active:
        raise HTTPException(400, "Connection is inactive")
    return await channel_service.check_and_respond(conn, session)

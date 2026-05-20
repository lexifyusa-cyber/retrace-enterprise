"""
Connections API — manage stored credentials for external data sources
(SFTP, SMB, S3, Azure Blob).

Routes:
  GET    /                  list connections in tenant
  POST   /                  create new
  GET    /schema/{type}     return per-type form schema for UI
  GET    /{id}              get one (without secrets)
  PATCH  /{id}              update name + config
  DELETE /{id}              delete
  POST   /{id}/test         re-test credentials
  GET    /{id}/list?path=…  browse remote dirs (used by AddGroupModal)
"""

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.db.database import get_session
from app.models.connection import Connection
from app.services.sources import (
    SUPPORTED_SOURCE_TYPES,
    SOURCE_CONFIG_SCHEMA,
    encrypt_config,
    decrypt_config,
    get_connector,
)

router = APIRouter()


# ─── Schemas ────────────────────────────────────────────────────────


class ConnectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    source_type: str = Field(..., pattern="^(sftp|smb|s3|azure_blob)$")  # OAuth sources go through /oauth/init
    config: dict[str, Any] = Field(...)


class ConnectionUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict[str, Any]] = None


# ─── Helpers ────────────────────────────────────────────────────────


def _validate_config(source_type: str, config: dict[str, Any]) -> None:
    schema = SOURCE_CONFIG_SCHEMA.get(source_type)
    if schema is None:
        raise HTTPException(400, f"Unsupported source_type: {source_type}")
    for field in schema:
        if field["required"] and not config.get(field["name"]):
            raise HTTPException(400, f"Missing required field: {field['name']}")


async def _load_connection_for_tenant(
    connection_id: str,
    session: AsyncSession,
    current_user: CurrentUser,
) -> Connection:
    res = await session.execute(
        select(Connection).where(Connection.connection_id == connection_id)
    )
    conn = res.scalar_one_or_none()
    if conn is None:
        raise HTTPException(404, "Connection not found")
    # Tenant scoping: zero_admin sees all, others see own tenant only
    if current_user.role != "zero_admin" and conn.tenant_id != current_user.tenant_id:
        raise HTTPException(404, "Connection not found")
    return conn


# ─── Routes ────────────────────────────────────────────────────────


@router.get("/schema/{source_type}")
async def get_schema(source_type: str):
    """Return the form schema for a given source type (for the UI)."""
    schema = SOURCE_CONFIG_SCHEMA.get(source_type)
    if schema is None:
        raise HTTPException(404, f"Unknown source_type: {source_type}")
    return {"source_type": source_type, "fields": schema}


@router.get("/")
async def list_connections(
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    q = select(Connection)
    if current_user.role != "zero_admin":
        q = q.where(Connection.tenant_id == current_user.tenant_id)
    res = await session.execute(q.order_by(Connection.created_at.desc()))
    return [c.to_dict() for c in res.scalars().all()]


@router.post("/", status_code=201)
async def create_connection(
    data: ConnectionCreate,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if data.source_type not in SUPPORTED_SOURCE_TYPES or data.source_type == "local":
        raise HTTPException(400, f"Cannot create connections of type {data.source_type!r}")
    _validate_config(data.source_type, data.config)

    conn = Connection(
        tenant_id=current_user.tenant_id,
        name=data.name,
        source_type=data.source_type,
        config_encrypted=encrypt_config(data.config),
        created_by=current_user.user_id,
    )
    # Test the connection before persisting so users get fast feedback
    try:
        connector = get_connector(data.source_type, data.config)
        ok, msg = connector.test()
        conn.last_tested_at = datetime.utcnow()
        conn.last_test_status = "ok" if ok else "error"
        conn.last_test_error = None if ok else msg
    except Exception as e:
        conn.last_tested_at = datetime.utcnow()
        conn.last_test_status = "error"
        conn.last_test_error = str(e)

    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return conn.to_dict()


@router.get("/{connection_id}")
async def get_connection(
    connection_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    conn = await _load_connection_for_tenant(connection_id, session, current_user)
    return conn.to_dict(include_config=True)


@router.patch("/{connection_id}")
async def update_connection(
    connection_id: str,
    data: ConnectionUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    conn = await _load_connection_for_tenant(connection_id, session, current_user)
    if data.name is not None:
        conn.name = data.name
    if data.config is not None:
        # Merge with existing — front-end can send partial config (e.g. just to
        # rotate a password). Secrets-only fields not in the new payload retain
        # their old value.
        existing = decrypt_config(conn.config_encrypted)
        merged = {**existing, **{k: v for k, v in data.config.items() if v is not None and v != ""}}
        _validate_config(conn.source_type, merged)
        conn.config_encrypted = encrypt_config(merged)
    conn.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(conn)
    return conn.to_dict()


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    conn = await _load_connection_for_tenant(connection_id, session, current_user)
    await session.delete(conn)
    await session.commit()


@router.post("/{connection_id}/test")
async def test_connection(
    connection_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    conn = await _load_connection_for_tenant(connection_id, session, current_user)
    config = decrypt_config(conn.config_encrypted)
    try:
        connector = get_connector(conn.source_type, config)
        ok, msg = connector.test()
    except Exception as e:
        ok, msg = False, str(e)
    conn.last_tested_at = datetime.utcnow()
    conn.last_test_status = "ok" if ok else "error"
    conn.last_test_error = None if ok else msg
    await session.commit()
    return {"ok": ok, "message": msg}


@router.get("/{connection_id}/list")
async def list_remote_dir(
    connection_id: str,
    path: str = Query(default="/"),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Browse remote directories. Used by AddGroupModal's path picker."""
    conn = await _load_connection_for_tenant(connection_id, session, current_user)
    config = decrypt_config(conn.config_encrypted)
    try:
        connector = get_connector(conn.source_type, config)
        entries = connector.list_dir(path)
        return {
            "path": path,
            "entries": [
                {"name": e.name, "path": e.path, "is_dir": e.is_dir, "size": e.size}
                for e in entries
            ],
        }
    except Exception as e:
        raise HTTPException(500, f"Listing failed: {e}")

"""
POD management API endpoints.
"""

from typing import Optional
from uuid import uuid4
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.core.security import (
    bearer_scheme,
    create_download_token,
    get_current_user,
    get_tenant_team_ids,
    verify_download_token,
    verify_token,
    CurrentUser,
)
from app.db.database import get_session
from app.models.pod import Pod
from app.services.pod_generator import generate_pod_installer
from app.services.websocket_manager import websocket_manager

logger = structlog.get_logger()
router = APIRouter()


class PodCreate(BaseModel):
    """Schema for creating a new POD."""
    pod_name: str = Field(..., min_length=1, max_length=255)
    target_os: str = Field(..., pattern="^(windows|macos|linux)$")
    custom_config: Optional[dict] = None


class PodResponse(BaseModel):
    """Schema for POD response."""
    pod_id: str
    pod_name: str
    machine_hostname: Optional[str]
    os_type: Optional[str]
    status: str
    last_heartbeat: Optional[str]
    created_at: Optional[str]
    metadata: dict = {}


class PodGenerateResponse(BaseModel):
    """Schema for POD generation response."""
    pod_id: str
    pod_name: str
    installer_path: str
    archive_name: str
    instructions: str
    binary_included: bool
    download_url: str


@router.get("", response_model=list[PodResponse])
async def list_pods(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List all registered PODs (excludes synthetic __local__ pod)."""
    tenant_teams = await get_tenant_team_ids(current_user, session)
    result = await session.execute(
        select(Pod)
        .where(Pod.pod_id != "__local__")
        .where(Pod.team_id.in_(tenant_teams))
    )
    pods = result.scalars().all()
    return [PodResponse(**pod.to_dict()) for pod in pods]


@router.post("/generate", response_model=PodGenerateResponse)
async def generate_pod(
    pod_data: PodCreate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate a new POD installer package."""
    # Create POD record
    pod_id = str(uuid4())
    pod = Pod(
        pod_id=pod_id,
        pod_name=pod_data.pod_name,
        os_type=pod_data.target_os,
        status="pending",
        metadata_json=pod_data.custom_config or {},
    )
    
    session.add(pod)
    await session.commit()
    
    logger.info(
        "Generating POD installer",
        pod_id=pod_id,
        pod_name=pod_data.pod_name,
        target_os=pod_data.target_os
    )
    
    # Generate installer package
    installer_info = await generate_pod_installer(
        pod_id=pod_id,
        pod_name=pod_data.pod_name,
        target_os=pod_data.target_os,
        custom_config=pod_data.custom_config
    )
    
    return PodGenerateResponse(
        pod_id=pod_id,
        pod_name=pod_data.pod_name,
        installer_path=installer_info["path"],
        archive_name=installer_info["archive_name"],
        instructions=installer_info["instructions"],
        binary_included=installer_info["binary_included"],
        download_url=installer_info["download_url"]
    )


@router.get("/{pod_id}", response_model=PodResponse)
async def get_pod(
    pod_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Get a specific POD by ID."""
    tenant_teams = await get_tenant_team_ids(current_user, session)
    result = await session.execute(
        select(Pod)
        .where(Pod.pod_id == pod_id)
        .where(Pod.team_id.in_(tenant_teams))
    )
    pod = result.scalar_one_or_none()

    if not pod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"POD {pod_id} not found"
        )

    return PodResponse(**pod.to_dict())


@router.delete("/{pod_id}")
async def delete_pod(
    pod_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete a POD and disconnect it."""
    tenant_teams = await get_tenant_team_ids(current_user, session)
    result = await session.execute(
        select(Pod)
        .where(Pod.pod_id == pod_id)
        .where(Pod.team_id.in_(tenant_teams))
    )
    pod = result.scalar_one_or_none()

    if not pod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"POD {pod_id} not found"
        )
    
    # Disconnect POD if connected
    await websocket_manager.disconnect_pod(pod_id)
    
    await session.delete(pod)
    await session.commit()
    
    logger.info("POD deleted", pod_id=pod_id)
    
    return {"status": "deleted", "pod_id": pod_id}


@router.get("/{pod_id}/status")
async def get_pod_status(
    pod_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get POD connection status."""
    is_connected = websocket_manager.is_pod_connected(pod_id)
    
    return {
        "pod_id": pod_id,
        "connected": is_connected,
        "status": "online" if is_connected else "offline"
    }


@router.post("/{pod_id}/download-token")
async def issue_pod_download_token(
    pod_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Issue a short-lived signed URL for downloading the pod installer.

    Background: the Teams page is embedded in an iframe at retrace.me/try.
    Cross-origin iframes can't reliably trigger downloads via the standard
    "fetch with Authorization header → blob → synthetic <a>.click()"
    pattern, because the iframe's transient user-activation flag is
    consumed by the await on the multi-megabyte fetch, and Chrome silently
    drops the synthetic blob click. The workaround is to do the auth check
    here (with the user's Bearer token) and hand back a URL that embeds a
    60-second JWT, so the actual download can be triggered with a plain
    `<a href download>` click — which the browser treats as a regular
    cross-origin navigation that the server returns as
    Content-Disposition: attachment, and which works inside iframes.
    """
    # Tenant check — same as the download endpoint's header-auth path.
    tenant_teams = await get_tenant_team_ids(current_user, session)
    result = await session.execute(
        select(Pod)
        .where(Pod.pod_id == pod_id)
        .where(Pod.team_id.in_(tenant_teams))
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"POD {pod_id} not found",
        )

    token = create_download_token(current_user.user_id, pod_id)
    return {"url": f"/api/v1/pods/{pod_id}/download?token={token}"}


def _resolve_installer_file(pod_id: str) -> Path:
    """Locate the pre-built installer archive for *pod_id* on disk.

    Raises 404 if the file is gone (e.g. the pod was generated before a
    container restart wiped pod-installers/).
    """
    installer_dir = Path("./pod-installers")
    pod_prefix = f"retrace-{pod_id[:8]}-"
    for ext in [".zip", ".tar.gz"]:
        for file in installer_dir.glob(f"{pod_prefix}*{ext}"):
            if file.exists():
                return file
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Installer package not found. Please regenerate the POD.",
    )


@router.get("/{pod_id}/download")
async def download_pod_installer(
    pod_id: str,
    token: Optional[str] = Query(
        None,
        description="Short-lived signed token (alternative to Authorization header)",
    ),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
):
    """Download the POD installer package.

    Two auth paths are accepted:

    * `?token=<jwt>` — short-lived signed URL issued by
      ``POST /api/v1/pods/{pod_id}/download-token``. Used by the Teams UI
      when embedded in cross-origin iframes (retrace.me/try), where a
      Bearer-header fetch + blob download is blocked by the browser. The
      token is scoped to this exact pod_id, expires in ~60s, and the
      tenant check was already enforced at issuance.

    * ``Authorization: Bearer <access_token>`` — used by direct visitors
      to retrace-trial.fly.dev. Tenant check is performed inline.
    """
    # Path 1: signed URL
    if token:
        if not verify_download_token(token, pod_id):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired download token",
            )
        result = await session.execute(select(Pod).where(Pod.pod_id == pod_id))
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"POD {pod_id} not found",
            )

    # Path 2: Bearer header — replicate get_current_user inline so we can
    # support either auth style on the same endpoint.
    elif credentials:
        payload = verify_token(credentials.credentials, expected_type="access")
        if not payload or "sub" not in payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )
        from app.models.user import User
        user = await session.get(User, payload["sub"])
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )
        current_user = CurrentUser(payload)
        tenant_teams = await get_tenant_team_ids(current_user, session)
        result = await session.execute(
            select(Pod)
            .where(Pod.pod_id == pod_id)
            .where(Pod.team_id.in_(tenant_teams))
        )
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"POD {pod_id} not found",
            )

    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    installer_file = _resolve_installer_file(pod_id)
    media_type = (
        "application/zip" if installer_file.suffix == ".zip" else "application/gzip"
    )
    return FileResponse(
        path=installer_file,
        media_type=media_type,
        filename=installer_file.name,
    )


@router.post("/{pod_id}/browse")
async def browse_pod_filesystem(
    pod_id: str,
    path: str = ".",
    recursive: bool = False,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Browse the filesystem on a POD machine (sandboxed to agent root directory)."""
    # Verify POD exists and belongs to tenant
    tenant_teams = await get_tenant_team_ids(current_user, session)
    result = await session.execute(
        select(Pod)
        .where(Pod.pod_id == pod_id)
        .where(Pod.team_id.in_(tenant_teams))
    )
    pod = result.scalar_one_or_none()

    if not pod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"POD {pod_id} not found"
        )
    
    # Check POD connection
    if not websocket_manager.is_pod_connected(pod_id):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"POD {pod_id} is not connected"
        )
    
    # Send RPC to POD
    try:
        response = await websocket_manager.call_pod_method(
            pod_id=pod_id,
            method="list_directory",
            params={
                "path": path,
                "recursive": recursive,
                "filters": {}
            }
        )
        return response
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="POD did not respond in time"
        )

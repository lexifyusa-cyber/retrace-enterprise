"""
POD management API endpoints.
"""

from typing import Optional
from uuid import uuid4
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

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
    session: AsyncSession = Depends(get_session)
):
    """List all registered PODs (excludes synthetic __local__ pod)."""
    result = await session.execute(select(Pod).where(Pod.pod_id != "__local__"))
    pods = result.scalars().all()
    return [PodResponse(**pod.to_dict()) for pod in pods]


@router.post("/generate", response_model=PodGenerateResponse)
async def generate_pod(
    pod_data: PodCreate,
    session: AsyncSession = Depends(get_session)
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
    session: AsyncSession = Depends(get_session)
):
    """Get a specific POD by ID."""
    result = await session.execute(
        select(Pod).where(Pod.pod_id == pod_id)
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
    session: AsyncSession = Depends(get_session)
):
    """Delete a POD and disconnect it."""
    result = await session.execute(
        select(Pod).where(Pod.pod_id == pod_id)
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
async def get_pod_status(pod_id: str):
    """Get POD connection status."""
    is_connected = websocket_manager.is_pod_connected(pod_id)
    
    return {
        "pod_id": pod_id,
        "connected": is_connected,
        "status": "online" if is_connected else "offline"
    }


@router.get("/{pod_id}/download")
async def download_pod_installer(
    pod_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Download the POD installer package."""
    # Verify POD exists
    result = await session.execute(
        select(Pod).where(Pod.pod_id == pod_id)
    )
    pod = result.scalar_one_or_none()
    
    if not pod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"POD {pod_id} not found"
        )
    
    # Find the installer file
    installer_dir = Path("./pod-installers")
    pod_prefix = f"retrace-{pod_id[:8]}-"
    
    # Look for .zip or .tar.gz file
    installer_file = None
    for ext in [".zip", ".tar.gz"]:
        for file in installer_dir.glob(f"{pod_prefix}*{ext}"):
            installer_file = file
            break
        if installer_file:
            break
    
    if not installer_file or not installer_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Installer package not found. Please regenerate the POD."
        )
    
    # Determine media type
    media_type = "application/zip" if installer_file.suffix == ".zip" else "application/gzip"
    
    return FileResponse(
        path=installer_file,
        media_type=media_type,
        filename=installer_file.name
    )


@router.post("/{pod_id}/browse")
async def browse_pod_filesystem(
    pod_id: str,
    path: str = "/",
    recursive: bool = False,
    session: AsyncSession = Depends(get_session)
):
    """Browse the filesystem on a POD machine."""
    # Verify POD exists
    result = await session.execute(
        select(Pod).where(Pod.pod_id == pod_id)
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

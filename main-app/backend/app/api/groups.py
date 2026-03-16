"""
Folder Group management API endpoints.
"""

from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from app.db.database import get_session
from app.models.pod import Pod
from app.models.folder_group import FolderGroup, FolderPath

logger = structlog.get_logger()
router = APIRouter()


class FolderPathCreate(BaseModel):
    """Schema for creating a folder path."""
    absolute_path: str
    scan_recursive: bool = True
    file_filters: Optional[dict] = Field(default_factory=lambda: {"include": [], "exclude": []})


class FolderGroupCreate(BaseModel):
    """Schema for creating a folder group."""
    pod_id: str
    group_name: str = Field(..., min_length=1, max_length=255)
    group_type: str = Field(
        default="code",
        pattern="^(code|documentation|tickets|other)$"
    )
    folder_paths: list[FolderPathCreate]


class FolderGroupUpdate(BaseModel):
    """Schema for updating a folder group."""
    group_name: Optional[str] = None
    group_type: Optional[str] = None
    folder_paths: Optional[list[FolderPathCreate]] = None


class FolderGroupResponse(BaseModel):
    """Schema for folder group response."""
    group_id: str
    pod_id: str
    group_name: str
    group_type: str
    namespace: str
    created_at: Optional[str]
    last_trained: Optional[str]
    training_status: str
    folder_paths: list[dict]
    metadata: dict = {}


@router.get("", response_model=list[FolderGroupResponse])
async def list_groups(
    pod_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session)
):
    """List all folder groups, optionally filtered by POD."""
    query = select(FolderGroup).options(selectinload(FolderGroup.folder_paths))
    
    if pod_id:
        query = query.where(FolderGroup.pod_id == pod_id)
    
    result = await session.execute(query)
    groups = result.scalars().all()
    
    return [FolderGroupResponse(**group.to_dict()) for group in groups]


@router.post("", response_model=FolderGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    group_data: FolderGroupCreate,
    session: AsyncSession = Depends(get_session)
):
    """Create a new folder group."""
    # Verify POD exists
    result = await session.execute(
        select(Pod).where(Pod.pod_id == group_data.pod_id)
    )
    pod = result.scalar_one_or_none()
    
    if not pod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"POD {group_data.pod_id} not found"
        )
    
    # Generate namespace
    namespace = f"pod-{group_data.pod_id[:8]}-{group_data.group_name.lower().replace(' ', '-')}"
    
    # Check namespace uniqueness
    existing = await session.execute(
        select(FolderGroup).where(FolderGroup.namespace == namespace)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Namespace {namespace} already exists"
        )
    
    # Create folder group
    group_id = str(uuid4())
    group = FolderGroup(
        group_id=group_id,
        pod_id=group_data.pod_id,
        group_name=group_data.group_name,
        group_type=group_data.group_type,
        namespace=namespace,
    )
    
    # Add folder paths
    for path_data in group_data.folder_paths:
        folder_path = FolderPath(
            path_id=str(uuid4()),
            group_id=group_id,
            absolute_path=path_data.absolute_path,
            scan_recursive=path_data.scan_recursive,
            file_filters=path_data.file_filters,
        )
        group.folder_paths.append(folder_path)
    
    session.add(group)
    await session.commit()
    
    # Reload with relationships
    result = await session.execute(
        select(FolderGroup)
        .options(selectinload(FolderGroup.folder_paths))
        .where(FolderGroup.group_id == group_id)
    )
    group = result.scalar_one()
    
    logger.info(
        "Folder group created",
        group_id=group_id,
        pod_id=group_data.pod_id,
        group_name=group_data.group_name
    )
    
    return FolderGroupResponse(**group.to_dict())


@router.get("/{group_id}", response_model=FolderGroupResponse)
async def get_group(
    group_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get a specific folder group by ID."""
    result = await session.execute(
        select(FolderGroup)
        .options(selectinload(FolderGroup.folder_paths))
        .where(FolderGroup.group_id == group_id)
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Folder group {group_id} not found"
        )
    
    return FolderGroupResponse(**group.to_dict())


@router.put("/{group_id}", response_model=FolderGroupResponse)
async def update_group(
    group_id: str,
    group_data: FolderGroupUpdate,
    session: AsyncSession = Depends(get_session)
):
    """Update a folder group."""
    result = await session.execute(
        select(FolderGroup)
        .options(selectinload(FolderGroup.folder_paths))
        .where(FolderGroup.group_id == group_id)
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Folder group {group_id} not found"
        )
    
    # Update fields
    if group_data.group_name is not None:
        group.group_name = group_data.group_name
    
    if group_data.group_type is not None:
        group.group_type = group_data.group_type
    
    if group_data.folder_paths is not None:
        # Remove existing paths
        for path in group.folder_paths:
            await session.delete(path)
        
        # Add new paths
        group.folder_paths = []
        for path_data in group_data.folder_paths:
            folder_path = FolderPath(
                path_id=str(uuid4()),
                group_id=group_id,
                absolute_path=path_data.absolute_path,
                scan_recursive=path_data.scan_recursive,
                file_filters=path_data.file_filters,
            )
            group.folder_paths.append(folder_path)
    
    await session.commit()
    await session.refresh(group)
    
    logger.info("Folder group updated", group_id=group_id)
    
    return FolderGroupResponse(**group.to_dict())


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Delete a folder group."""
    result = await session.execute(
        select(FolderGroup).where(FolderGroup.group_id == group_id)
    )
    group = result.scalar_one_or_none()
    
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Folder group {group_id} not found"
        )
    
    await session.delete(group)
    await session.commit()
    
    logger.info("Folder group deleted", group_id=group_id)
    
    return {"status": "deleted", "group_id": group_id}



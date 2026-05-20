"""
Team management API endpoints.
"""

from typing import Optional

from app.api.files import is_path_allowed
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from app.db.database import get_session
from app.models.team import Team
from app.models.folder_group import FolderGroup, FolderPath
from app.models.pod import Pod
from app.models.team_access import TeamAccess
from app.models.tenant import Tenant
from app.models.user import User
from app.core.security import get_optional_user, get_current_user, CurrentUser
from sqlalchemy import func

logger = structlog.get_logger()
router = APIRouter()


class FolderPathCreate(BaseModel):
    """Schema for creating a folder path."""
    absolute_path: str
    scan_recursive: bool = True
    # Optional remote-source connection. NULL = local filesystem (or remote
    # POD agent). Non-null = path lives inside a Connection (SFTP/SMB/S3/Azure).
    connection_id: Optional[str] = None
    file_filters: Optional[dict] = Field(default_factory=lambda: {"include": [], "exclude": []})


class FolderGroupCreate(BaseModel):
    """Schema for creating a folder group within a team."""
    pod_id: str
    group_name: str = Field(..., min_length=1, max_length=255)
    group_type: str = Field(
        default="code",
        pattern="^(code|documentation|tickets|other)$"
    )
    folder_paths: list[FolderPathCreate]


class TeamCreate(BaseModel):
    """Schema for creating a team."""
    team_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    auto_generate_description: bool = Field(default=True)
    folder_groups: list[FolderGroupCreate] = Field(default_factory=list)


class TeamUpdate(BaseModel):
    """Schema for updating a team."""
    team_name: Optional[str] = None
    description: Optional[str] = None


class QAPair(BaseModel):
    """A single question-answer pair from an agent conversation."""
    question: str
    answer: str


class SyncQARequest(BaseModel):
    """Request body for syncing agent Q&A pairs to training data."""
    qa_pairs: list[QAPair] = Field(..., min_length=1, max_length=10)


class FolderGroupResponse(BaseModel):
    """Schema for folder group response."""
    group_id: str
    team_id: str
    pod_id: str
    group_name: str
    group_type: str
    namespace: str
    created_at: Optional[str]
    last_trained: Optional[str]
    training_status: str
    folder_paths: list[dict]
    metadata: dict = {}


class TeamResponse(BaseModel):
    """Schema for team response."""
    team_id: str
    team_name: str
    description: Optional[str]
    auto_generate_description: bool = True
    created_at: Optional[str]
    folder_groups: list[FolderGroupResponse]
    metadata: dict = {}


@router.get("", response_model=list[TeamResponse])
async def list_teams(
    session: AsyncSession = Depends(get_session),
    current_user: Optional[CurrentUser] = Depends(get_optional_user),
):
    """List teams. Admins see all; user_admins see own teams; users see manager's teams."""
    query = select(Team).options(
        selectinload(Team.folder_groups).selectinload(FolderGroup.folder_paths)
    )

    if current_user and current_user.role == "zero_admin":
        pass  # System-level admin sees all teams
    elif current_user and current_user.role in ("admin", "super_admin", "tenant_admin"):
        # Tenant admins see their tenant's teams + prebuilt sample teams
        from sqlalchemy import or_, text
        query = query.where(
            or_(
                Team.tenant_id == current_user.tenant_id,
                text("json_extract(teams.metadata_json, '$.is_prebuilt') = 1"),
            )
        )
    elif current_user and current_user.role == "user_admin":
        # User admins see teams they created + prebuilt sample teams
        from sqlalchemy import or_, text
        query = query.where(
            or_(
                Team.created_by == current_user.user_id,
                text("json_extract(teams.metadata_json, '$.is_prebuilt') = 1"),
            )
        )
    elif current_user:
        # Regular users see teams they have explicit access to + prebuilt sample teams
        from sqlalchemy import or_, text
        accessible = select(TeamAccess.team_id).where(TeamAccess.user_id == current_user.user_id)
        query = query.where(
            or_(
                Team.team_id.in_(accessible),
                text("json_extract(teams.metadata_json, '$.is_prebuilt') = 1"),
            )
        )

    result = await session.execute(query)
    teams = result.scalars().all()

    return [TeamResponse(**team.to_dict()) for team in teams]


@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    team_data: TeamCreate,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create a new team. Admins: unlimited. User admins: subject to tenant team limit."""
    if not current_user.has_role("user_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins and user admins can create teams")

    # Enforce team limit for user_admin (not full admin)
    if current_user.role == "user_admin" and current_user.tenant_id:
        count_result = await session.execute(
            select(func.count(Team.team_id)).where(Team.created_by == current_user.user_id)
        )
        current_count = count_result.scalar() or 0
        user = await session.get(User, current_user.user_id)
        tenant = await session.get(Tenant, current_user.tenant_id)
        # Per-user limit overrides tenant default when set
        max_allowed = (
            user.max_teams
            if user and user.max_teams is not None
            else (tenant.max_teams_per_user_admin if tenant else 10)
        )
        if current_count >= max_allowed:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Team limit reached ({max_allowed}). Contact your admin to increase the limit.",
            )

    team_id = str(uuid4())
    team = Team(
        team_id=team_id,
        tenant_id=current_user.tenant_id,
        created_by=current_user.user_id,
        team_name=team_data.team_name,
        description=team_data.description,
        auto_generate_description=team_data.auto_generate_description,
    )

    # Add team to session first so it's available for relationships
    session.add(team)

    # Add folder groups if provided
    for group_data in team_data.folder_groups:
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

        # For __local__ pod, validate paths are under allowed browse roots —
        # but only for paths NOT coming from a remote source connection.
        # Remote-source paths (SFTP/SMB/S3/Azure) live inside their connection
        # and are staged into a temp dir at training time.
        if group_data.pod_id == "__local__":
            for path_data in group_data.folder_paths:
                if path_data.connection_id:
                    continue  # validated server-side via connection ACL
                if not is_path_allowed(path_data.absolute_path):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Path is not under allowed browse roots: {path_data.absolute_path}",
                    )

        # Generate namespace
        namespace = f"team-{team_id[:8]}-{group_data.group_name.lower().replace(' ', '-')}"

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
            team_id=team_id,
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
                connection_id=path_data.connection_id,
                file_filters=path_data.file_filters,
            )
            group.folder_paths.append(folder_path)

        # Add group to session - relationship maintained via team_id foreign key
        session.add(group)

    # Auto-create TeamAccess for the creator
    session.add(TeamAccess(
        team_id=team_id,
        user_id=current_user.user_id,
        permission="admin",
        granted_by=current_user.user_id,
    ))

    # Auto-grant access to users managed by this user_admin (not all tenant users)
    if current_user.role == "user_admin":
        managed_users_result = await session.execute(
            select(User.user_id).where(
                User.manager_id == current_user.user_id,
            )
        )
        for (uid,) in managed_users_result.all():
            session.add(TeamAccess(
                team_id=team_id,
                user_id=uid,
                permission="view",
                granted_by=current_user.user_id,
            ))

    await session.commit()

    # Reload with relationships
    result = await session.execute(
        select(Team)
        .options(
            selectinload(Team.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Team.team_id == team_id)
    )
    team = result.scalar_one()

    logger.info(
        "Team created",
        team_id=team_id,
        team_name=team_data.team_name
    )

    return TeamResponse(**team.to_dict())


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Get a specific team by ID."""
    query = (
        select(Team)
        .options(
            selectinload(Team.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Team.team_id == team_id)
    )
    # Tenant isolation: non-system admins can only see their tenant's teams
    if current_user.role != "zero_admin" and current_user.tenant_id:
        query = query.where(Team.tenant_id == current_user.tenant_id)

    result = await session.execute(query)
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

    return TeamResponse(**team.to_dict())


@router.put("/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: str,
    team_data: TeamUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Update a team."""
    query = (
        select(Team)
        .options(
            selectinload(Team.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Team.team_id == team_id)
    )
    if current_user.role != "zero_admin" and current_user.tenant_id:
        query = query.where(Team.tenant_id == current_user.tenant_id)

    result = await session.execute(query)
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

    # Update fields
    if team_data.team_name is not None:
        team.team_name = team_data.team_name

    if team_data.description is not None:
        team.description = team_data.description

    await session.commit()
    await session.refresh(team)

    logger.info("Team updated", team_id=team_id)

    return TeamResponse(**team.to_dict())


@router.delete("/{team_id}")
async def delete_team(
    team_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete a team."""
    query = select(Team).where(Team.team_id == team_id)
    if current_user.role != "zero_admin" and current_user.tenant_id:
        query = query.where(Team.tenant_id == current_user.tenant_id)

    result = await session.execute(query)
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

    # Prevent deletion of prebuilt example teams
    if (team.metadata_json or {}).get("is_prebuilt"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Prebuilt example teams cannot be deleted."
        )

    await session.delete(team)
    await session.commit()

    logger.info("Team deleted", team_id=team_id)

    return {"status": "deleted", "team_id": team_id}


# ---------------------------------------------------------------------------
# Team-scoped POD Agent management
# ---------------------------------------------------------------------------


@router.post("/{team_id}/generate-pod")
async def generate_team_pod(
    team_id: str,
    target_os: str = "linux",
    agent_name: str = "",
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Generate a POD agent installer for this team.

    The agent_name is baked into the config so no setup is needed on first run.
    """
    from app.services.pod_generator import generate_pod_installer

    # Verify team exists
    result = await session.execute(
        select(Team).where(Team.team_id == team_id)
    )
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

    # Validate target_os
    if target_os not in ("windows", "linux"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_os must be one of: windows, linux"
        )

    # Validate agent_name
    agent_name = agent_name.strip()
    if not agent_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="agent_name is required"
        )

    # Check for duplicate agent name
    existing = await session.execute(
        select(Pod).where(Pod.pod_name == agent_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent name '{agent_name}' is already taken. Choose a different name."
        )

    # Create POD record linked to this team
    pod_id = str(uuid4())
    pod = Pod(
        pod_id=pod_id,
        team_id=team_id,
        pod_name=agent_name,
        os_type=target_os,
        status="pending",
        metadata_json={"team_name": team.team_name},
    )
    session.add(pod)
    await session.commit()

    logger.info(
        "Generating team POD installer",
        team_id=team_id,
        pod_id=pod_id,
        agent_name=agent_name,
        target_os=target_os,
    )

    # Generate installer package with the name baked in
    installer_info = await generate_pod_installer(
        pod_id=pod_id,
        pod_name=agent_name,
        target_os=target_os,
        custom_config={"team_id": team_id},
    )

    return {
        "pod_id": pod_id,
        "team_id": team_id,
        "agent_name": agent_name,
        "target_os": target_os,
        "archive_name": installer_info["archive_name"],
        "binary_included": installer_info["binary_included"],
        "download_url": installer_info["download_url"],
        "instructions": installer_info["instructions"],
        "ws_host": installer_info.get("ws_host", "localhost"),
    }


@router.get("/{team_id}/pods")
async def list_team_pods(
    team_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    """List all POD agents associated with this team."""
    result = await session.execute(
        select(Pod).where(Pod.team_id == team_id)
    )
    pods = result.scalars().all()

    return {
        "team_id": team_id,
        "pods": [p.to_dict() for p in pods],
        "total": len(pods),
        "online": sum(1 for p in pods if p.status == "online"),
    }


@router.post("/{team_id}/groups", response_model=FolderGroupResponse, status_code=status.HTTP_201_CREATED)
async def add_folder_group(
    team_id: str,
    group_data: FolderGroupCreate,
    session: AsyncSession = Depends(get_session)
):
    """Add a folder group to a team."""
    # Verify team exists and eagerly load folder_groups relationship
    result = await session.execute(
        select(Team)
        .options(selectinload(Team.folder_groups))
        .where(Team.team_id == team_id)
    )
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

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
    namespace = f"team-{team_id[:8]}-{group_data.group_name.lower().replace(' ', '-')}"

    # Check namespace uniqueness
    existing = await session.execute(
        select(FolderGroup).where(FolderGroup.namespace == namespace)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Namespace {namespace} already exists"
        )

    # For __local__ pod, validate paths are under allowed browse roots
    if group_data.pod_id == "__local__":
        for path_data in group_data.folder_paths:
            if not is_path_allowed(path_data.absolute_path):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Path is not under allowed browse roots: {path_data.absolute_path}",
                )

    # Create folder group
    group_id = str(uuid4())
    group = FolderGroup(
        group_id=group_id,
        team_id=team_id,
        pod_id=group_data.pod_id,
        group_name=group_data.group_name,
        group_type=group_data.group_type,
        namespace=namespace,
    )

    # For __local__ pod, validate paths are under allowed browse roots
    if group_data.pod_id == "__local__":
        for path_data in group_data.folder_paths:
            if not is_path_allowed(path_data.absolute_path):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Path is not under allowed browse roots: {path_data.absolute_path}",
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

    # Add group to session - the relationship will be maintained via team_id foreign key
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
        "Folder group added to team",
        team_id=team_id,
        group_id=group_id,
        group_name=group_data.group_name
    )

    return FolderGroupResponse(**group.to_dict())


@router.delete("/{team_id}/groups/{group_id}")
async def remove_folder_group(
    team_id: str,
    group_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Remove a folder group from a team."""
    result = await session.execute(
        select(FolderGroup).where(
            FolderGroup.group_id == group_id,
            FolderGroup.team_id == team_id
        )
    )
    group = result.scalar_one_or_none()

    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Folder group {group_id} not found in team {team_id}"
        )

    await session.delete(group)
    await session.commit()

    logger.info("Folder group removed from team", team_id=team_id, group_id=group_id)

    return {"status": "deleted", "group_id": group_id}


@router.post("/{team_id}/train")
async def train_team(
    team_id: str,
    force_full: bool = False,
    session: AsyncSession = Depends(get_session)
):
    """Train a team – runs the RAG pipeline over all its folder groups.

    By default training is **incremental**: only new and modified files
    are processed.  Pass ``?force_full=true`` to wipe the existing index
    and re-process every file from scratch.
    """
    import asyncio
    from app.rag.pipeline import run_team_training

    # Get team with all folder groups and their paths
    result = await session.execute(
        select(Team)
        .options(
            selectinload(Team.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Team.team_id == team_id)
    )
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

    if not team.folder_groups:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Team has no folder groups to train"
        )

    # Collect all active folder paths (with optional connection_id for remote sources)
    # Each item is (absolute_path, connection_id_or_None).
    folder_specs: list[tuple[str, Optional[str]]] = []
    group_ids: list[str] = []
    for group in team.folder_groups:
        if group.training_status == "training":
            continue
        group_ids.append(group.group_id)
        for fp in group.folder_paths:
            if fp.is_active:
                folder_specs.append((fp.absolute_path, fp.connection_id))

    if not folder_specs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active folder paths found (or all groups are already training)."
        )

    # Mark groups as training
    for group in team.folder_groups:
        if group.group_id in group_ids:
            group.training_status = "training"
    await session.commit()

    # Launch the 4-phase pipeline in the background. The wrapper stages any
    # remote-source paths to a temp dir first, then runs the local pipeline.
    asyncio.create_task(
        _train_with_remote_staging(
            team_id=team_id,
            folder_specs=folder_specs,
            group_ids=group_ids,
            team_name=team.team_name,
            team_description=team.description,
            force_full=force_full,
        )
    )

    logger.info(
        "team_training_launched",
        team_id=team_id,
        team_name=team.team_name,
        groups=len(group_ids),
        paths=len(folder_specs),
        force_full=force_full,
    )

    return {
        "status": "started",
        "team_id": team_id,
        "folder_groups": len(group_ids),
        "folder_paths": len(folder_specs),
        "incremental": not force_full,
    }


@router.post("/{team_id}/stop-training")
async def stop_training(
    team_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Stop an in-progress training for a team.

    If the background task is still running, cancels it.
    Also resets any stuck 'training' group statuses back to 'pending'.
    """
    from app.rag.pipeline import _active_training_tasks

    task = _active_training_tasks.get(team_id)
    if task and not task.done():
        task.cancel()
        logger.info("team_training_task_cancelled", team_id=team_id)

    # Force-reset any groups stuck in "training" status for this team
    result = await session.execute(
        select(Team)
        .options(selectinload(Team.folder_groups))
        .where(Team.team_id == team_id)
    )
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

    reset_count = 0
    for group in team.folder_groups:
        if group.training_status == "training":
            group.training_status = "pending"
            # Preserve existing metadata but update phase
            meta = group.metadata_json or {}
            existing_logs = meta.get("logs", [])
            from datetime import datetime as dt
            group.metadata_json = {
                **meta,
                "phase": "cancelled",
                "message": "Training stopped by user",
                "logs": existing_logs + [f"[{dt.utcnow().strftime('%H:%M:%S')}] 🛑 Training stopped by user"],
            }
            reset_count += 1

    await session.commit()
    logger.info("team_training_stopped", team_id=team_id, groups_reset=reset_count)

    return {"status": "stopped", "team_id": team_id, "groups_reset": reset_count}


@router.post("/{team_id}/sync-qa")
async def sync_qa_to_training(
    team_id: str,
    body: SyncQARequest,
    session: AsyncSession = Depends(get_session),
):
    """Sync agent Q&A pairs into the team's training index.

    Embedding-based sync is disabled. Use KB-based training instead.
    """
    result = await session.execute(
        select(Team).where(Team.team_id == team_id)
    )
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found",
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Q&A sync to embedding index is disabled. Use KB-based training.",
    )


@router.get("/{team_id}/training-status")
async def get_training_status(
    team_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get real-time training status for a team's folder groups."""
    # Expire all cached objects so we read fresh progress from the DB
    session.expire_all()

    result = await session.execute(
        select(Team)
        .options(
            selectinload(Team.folder_groups).selectinload(FolderGroup.folder_paths)
        )
        .where(Team.team_id == team_id)
    )
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

    groups_status = []
    for group in team.folder_groups:
        meta = group.metadata_json if hasattr(group, 'metadata_json') and group.metadata_json else {}
        groups_status.append({
            "group_id": group.group_id,
            "group_name": group.group_name,
            "group_type": group.group_type,
            "training_status": group.training_status,
            "last_trained": str(group.last_trained) if group.last_trained else None,
            "folder_paths": [fp.absolute_path for fp in group.folder_paths if fp.is_active],
            "stats": meta,
        })

    is_any_training = any(g["training_status"] == "training" for g in groups_status)
    all_completed = all(g["training_status"] == "completed" for g in groups_status) and len(groups_status) > 0
    any_failed = any(g["training_status"] == "failed" for g in groups_status)

    return {
        "team_id": team_id,
        "team_name": team.team_name,
        "is_training": is_any_training,
        "all_completed": all_completed,
        "any_failed": any_failed,
        "groups": groups_status,
    }


@router.get("/{team_id}/training-tree")
async def get_training_tree(
    team_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Return the training data as a hierarchical tree for visualization.

    Reads the real KnowledgeBase blob and builds the tree from file_analysis,
    folder_structure, and files stored during training.
    """
    result = await session.execute(
        select(Team).where(Team.team_id == team_id)
    )
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

    # Load the actual KB data
    from app.rag.kb_store import KnowledgeBaseStore
    kb_store = KnowledgeBaseStore()
    kb_data = await kb_store.load(team_id)

    if not kb_data:
        return {
            "team_id": team_id,
            "team_name": team.team_name,
            "total_chunks": 0,
            "total_files": 0,
            "tree": [],
        }

    files = kb_data.get("files", {})
    file_analysis = kb_data.get("file_analysis", {})
    chunk_map = kb_data.get("chunk_map", {})

    # Classify each file into a type using file_analysis or extension
    type_buckets: dict[str, dict[str, list]] = {}  # {type: {sub_category: [file_info]}}

    for file_path, file_data in files.items():
        # Determine processing type from file_analysis or extension
        fa = file_analysis.get(file_path, {})
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""

        # Guess type from component/role or extension
        if ext in ("py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp", "h", "cs", "rb", "php", "swift", "kt"):
            ptype = "code"
        elif ext in ("md", "txt", "rst", "adoc", "docx", "pdf", "html"):
            ptype = "doc"
        elif ext in ("png", "jpg", "jpeg", "gif", "svg", "webp", "bmp"):
            ptype = "diagram_image"
        elif ext in ("csv", "json", "xml", "yaml", "yml", "toml", "ini", "cfg", "conf"):
            ptype = "doc"
        else:
            ptype = "other"

        # Derive sub-category from folder path
        parts = file_path.replace("\\", "/").split("/")
        if len(parts) > 1:
            sub_cat = parts[-2] if len(parts) >= 2 else "root"
        else:
            sub_cat = "root"

        # Count chunks for this file
        file_chunk_count = sum(
            1 for cm in chunk_map.values()
            if isinstance(cm, dict) and cm.get("file") == file_path
        )

        file_info = {
            "path": file_path,
            "name": parts[-1] if parts else file_path,
            "chunks": file_chunk_count,
        }

        if ptype not in type_buckets:
            type_buckets[ptype] = {}
        if sub_cat not in type_buckets[ptype]:
            type_buckets[ptype][sub_cat] = []
        type_buckets[ptype][sub_cat].append(file_info)

    # Also count non-file chunks (folder_analysis, project_analysis)
    analysis_chunks = sum(
        1 for cm in chunk_map.values()
        if isinstance(cm, dict) and cm.get("type") in ("folder_analysis", "project_analysis", "file_analysis")
    )
    if analysis_chunks > 0:
        if "summary" not in type_buckets:
            type_buckets["summary"] = {}
        type_buckets["summary"]["auto-generated"] = [{
            "path": "(analysis)",
            "name": f"{analysis_chunks} analysis entries",
            "chunks": analysis_chunks,
        }]

    # Build tree response
    tree = []
    total_chunks = len(chunk_map)
    total_files = len(files)

    for ptype, sub_cats in type_buckets.items():
        type_chunks = sum(f["chunks"] for fs in sub_cats.values() for f in fs)
        type_file_count = sum(len(fs) for fs in sub_cats.values())
        sub_categories = []
        for sub_name, file_list in sorted(sub_cats.items()):
            sub_chunks = sum(f["chunks"] for f in file_list)
            sub_categories.append({
                "name": sub_name,
                "chunks": sub_chunks,
                "file_count": len(file_list),
                "files": sorted(file_list, key=lambda x: x["name"]),
            })
        tree.append({
            "type": ptype,
            "chunks": type_chunks,
            "file_count": type_file_count,
            "sub_categories": sub_categories,
        })

    # Sort: code first, then doc, then others
    type_order = {"code": 0, "doc": 1, "summary": 2, "diagram_image": 3, "other": 4}
    tree.sort(key=lambda t: type_order.get(t["type"], 99))

    return {
        "team_id": team_id,
        "team_name": team.team_name,
        "total_chunks": total_chunks,
        "total_files": total_files,
        "tree": tree,
    }


@router.get("/{team_id}/chunks")
async def list_team_chunks(
    team_id: str,
    processing_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_session)
):
    """List indexed chunks for a team. Returns empty list when embedding index is not used."""
    result = await session.execute(
        select(Team).where(Team.team_id == team_id)
    )
    team = result.scalar_one_or_none()

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found"
        )

    return {
        "team_id": team_id,
        "team_name": team.team_name,
        "total": 0,
        "limit": limit,
        "offset": offset,
        "chunks": [],
    }


# ---------------------------------------------------------------------------
# Remote-source staging wrapper
# ---------------------------------------------------------------------------

async def _train_with_remote_staging(
    team_id: str,
    folder_specs: list[tuple[str, Optional[str]]],
    group_ids: list[str],
    team_name: str,
    team_description: Optional[str],
    force_full: bool,
):
    """Stage remote-source paths into a tenant-local temp dir, then run the
    standard local pipeline against the staged copies.

    For each (absolute_path, connection_id):
      - connection_id is None  → the path is local; pass it through unchanged.
      - connection_id is set   → look up the Connection, create a connector,
                                 download the path into a temp staging dir,
                                 and pass the staged local path to the pipeline.
    """
    import tempfile, asyncio as _aio
    from app.db.database import async_session_maker
    from app.models.connection import Connection
    from app.services.sources import get_connector, decrypt_config
    from app.rag.pipeline import run_team_training as _run_local

    staging_root = tempfile.mkdtemp(prefix=f"retrace-train-{team_id[:8]}-")
    local_paths: list[str] = []

    # Cache decoded connectors per connection_id (one connector per source)
    connectors: dict[str, object] = {}

    async with async_session_maker() as session:
        # Bulk-load connections referenced by this run
        connection_ids = {cid for _, cid in folder_specs if cid}
        if connection_ids:
            res = await session.execute(
                select(Connection).where(Connection.connection_id.in_(connection_ids))
            )
            for conn in res.scalars().all():
                try:
                    config = decrypt_config(conn.config_encrypted)
                    connectors[conn.connection_id] = get_connector(conn.source_type, config)
                except Exception as e:
                    print(f"[STAGING] Failed to init connector {conn.name}: {e}", flush=True)

    # Stage each remote path off the event loop (paramiko/boto3 are blocking)
    loop = _aio.get_event_loop()
    for path, connection_id in folder_specs:
        if not connection_id:
            local_paths.append(path)
            continue
        connector = connectors.get(connection_id)
        if connector is None:
            print(f"[STAGING] Skipping {path}: connector unavailable", flush=True)
            continue
        try:
            staged = await loop.run_in_executor(
                None,
                lambda c=connector, p=path: c.download_to(p, staging_root, recursive=True),
            )
            local_paths.append(staged)
        except Exception as e:
            print(f"[STAGING] Failed to stage {path} from connection {connection_id}: {e}", flush=True)

    # Close any connectors that opened persistent sessions
    for c in connectors.values():
        try:
            close = getattr(c, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    if not local_paths:
        print(f"[STAGING] No paths staged for training (team={team_id})", flush=True)
        return

    await _run_local(
        team_id=team_id,
        folder_paths=local_paths,
        group_ids=group_ids,
        team_name=team_name,
        team_description=team_description,
        force_full=force_full,
    )

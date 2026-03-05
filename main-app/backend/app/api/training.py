"""
Training job management API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.db.database import get_session
from app.models.training import TrainingJob

logger = structlog.get_logger()
router = APIRouter()


class TrainingJobResponse(BaseModel):
    """Schema for training job response."""
    job_id: str
    group_id: str
    status: str
    started_at: str | None
    completed_at: str | None
    progress_data: dict
    statistics: dict
    error_log: str | None
    created_at: str | None


@router.get("/jobs", response_model=list[TrainingJobResponse])
async def list_training_jobs(
    group_id: str | None = None,
    status_filter: str | None = None,
    session: AsyncSession = Depends(get_session)
):
    """List all training jobs, optionally filtered."""
    query = select(TrainingJob)
    
    if group_id:
        query = query.where(TrainingJob.group_id == group_id)
    
    if status_filter:
        query = query.where(TrainingJob.status == status_filter)
    
    query = query.order_by(TrainingJob.created_at.desc())
    
    result = await session.execute(query)
    jobs = result.scalars().all()
    
    return [TrainingJobResponse(**job.to_dict()) for job in jobs]


@router.get("/jobs/{job_id}", response_model=TrainingJobResponse)
async def get_training_job(
    job_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get a specific training job by ID."""
    result = await session.execute(
        select(TrainingJob).where(TrainingJob.job_id == job_id)
    )
    job = result.scalar_one_or_none()
    
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Training job {job_id} not found"
        )
    
    return TrainingJobResponse(**job.to_dict())


@router.post("/jobs/{job_id}/cancel")
async def cancel_training_job(
    job_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Cancel a running training job."""
    result = await session.execute(
        select(TrainingJob).where(TrainingJob.job_id == job_id)
    )
    job = result.scalar_one_or_none()
    
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Training job {job_id} not found"
        )
    
    if job.status not in ["queued", "running"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel job with status {job.status}"
        )
    
    job.status = "cancelled"
    await session.commit()
    
    logger.info("Training job cancelled", job_id=job_id)
    
    return {"status": "cancelled", "job_id": job_id}

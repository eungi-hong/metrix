"""GET /jobs/{id} -- the state of one queued job."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import SessionDep
from app.models.jobs import Job
from app.schemas.jobs import JobOut

router = APIRouter(tags=["jobs"])


@router.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    summary="Status, attempts, progress and errors for one background job",
)
async def get_job(job_id: int, session: SessionDep) -> JobOut:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}.")
    return JobOut.model_validate(job)

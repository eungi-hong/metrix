"""GET /jobs/{id} -- the state of one queued job.

A caller sees only jobs their own requests created or joined; anything else,
including every nightly job, answers 404 as if it did not exist. With a valid
X-Admin-Token, any job.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import AdminView, CurrentUser, SessionDep
from app.models.jobs import Job
from app.schemas.jobs import JobOut
from app.services import queue

router = APIRouter(tags=["jobs"])


@router.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    summary="Status, attempts, progress and errors for one background job",
)
async def get_job(
    job_id: int, session: SessionDep, principal: CurrentUser, admin: AdminView
) -> JobOut:
    job = await session.get(Job, job_id)
    if job is None or not (admin or await queue.is_requester(session, job.id, principal.key)):
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}.")
    return JobOut.model_validate(job)

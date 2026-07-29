from fastapi import APIRouter, Depends, Query, status

from app.dependencies import get_job_service
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
def list_jobs(
    project_id: str | None = Query(default=None),
    status_value: str | None = Query(default=None, alias="status"),
    job_type: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: JobService = Depends(get_job_service),
) -> list[dict]:
    return service.list(
        project_id=project_id,
        status=status_value,
        job_type=job_type,
        batch_id=batch_id,
        limit=limit,
        offset=offset,
    )


@router.get("/batches/{batch_id}")
def get_job_batch(
    batch_id: str,
    service: JobService = Depends(get_job_service),
) -> dict:
    return service.get_batch(batch_id)


@router.get("/{job_id}")
def get_job(
    job_id: str,
    service: JobService = Depends(get_job_service),
) -> dict:
    return service.get(job_id)


@router.post("/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_job(
    job_id: str,
    service: JobService = Depends(get_job_service),
) -> dict:
    return service.retry(job_id)


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: str,
    service: JobService = Depends(get_job_service),
) -> dict:
    return service.cancel(job_id)

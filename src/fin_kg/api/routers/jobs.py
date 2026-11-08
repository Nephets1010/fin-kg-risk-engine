"""Job endpoints: start and poll the long-running ingestion and scoring jobs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from fin_kg.api.deps import JobStoreDep, Repository, get_source_adapters, require_api_key
from fin_kg.api.schemas import (
    ErrorResponse,
    IngestReportResponse,
    JobRequest,
    JobStatusResponse,
)

router = APIRouter(
    prefix="/api/v1/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_api_key)],
)

ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "API Key 缺失或错误"},
    404: {"model": ErrorResponse, "description": "任务不存在"},
    422: {"model": ErrorResponse, "description": "参数不合法"},
}


def _status_response(job) -> JobStatusResponse:
    return JobStatusResponse(**job.as_dict())


@router.post(
    "/ingest",
    response_model=JobStatusResponse,
    status_code=202,
    summary="启动数据接入任务",
    responses=ERROR_RESPONSES,
)
def start_ingest(
    request: JobRequest,
    store: JobStoreDep,
    repository: Repository,
) -> JobStatusResponse:
    """Start an ingestion run and return its job id immediately.

    This is the ``Init`` step of the ``Init → Step → Poll`` protocol. Poll
    ``GET /api/v1/jobs/{jobId}`` for the status.
    """
    adapters = get_source_adapters()
    source = adapters.get(request.source)
    if source is None:
        raise ValueError(f"unknown source '{request.source}'; available: {sorted(adapters)}")

    job = store.create("ingest")

    def work(_job) -> dict:
        from datetime import date

        from fin_kg.ingestion.pipeline import IngestionPipeline

        since = date.fromisoformat(request.since) if request.since else None
        pipeline = IngestionPipeline(repository, batch_size=2000, checkpoint_dir=None)
        report = pipeline.run([source], since=since, limit=request.limit)
        return report.as_dict()

    store.run_async(job.id, work)
    return _status_response(store.get(job.id))


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="查询任务状态",
    responses=ERROR_RESPONSES,
)
def job_status(job_id: str, store: JobStoreDep) -> JobStatusResponse:
    """Return the current status of a job."""
    return _status_response(store.get(job_id))


@router.get(
    "/{job_id}/result",
    response_model=IngestReportResponse,
    summary="查询任务结果",
    responses=ERROR_RESPONSES,
)
def job_result(job_id: str, store: JobStoreDep) -> IngestReportResponse:
    """Return the result of a finished job."""
    job = store.get(job_id)
    if job.status != "succeeded" or job.result is None:
        return IngestReportResponse()
    return IngestReportResponse(**job.result)


@router.get("", response_model=list[JobStatusResponse], summary="列出任务")
def list_jobs(
    store: JobStoreDep, limit: int = Query(default=20, ge=1, le=100)
) -> list[JobStatusResponse]:
    """List the most recent jobs."""
    return [_status_response(job) for job in store.list(limit=limit)]


__all__ = ["router"]

"""Job management for long-running operations.

Ingestion and full-graph rescoring take minutes to hours at production
scale. They must not run inside an HTTP request, so they are exposed as
jobs with an ``Init → Step → Poll`` contract:

* ``POST /api/v1/jobs/ingest`` returns a job id immediately.
* ``GET  /api/v1/jobs/{id}`` reports the status and the progress.
* ``GET  /api/v1/jobs/{id}/result`` returns the final report.

The store is in-memory by default. A Redis-backed store is provided for
multi-worker deployments, where the job state must be visible to whichever
worker receives the polling request.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fin_kg.core.errors import NotFoundError
from fin_kg.core.logging import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Job:
    """The state of one long-running job."""

    id: str
    job_type: str
    status: str = "pending"  # pending | running | succeeded | failed | cancelled
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    progress: int = 0
    total: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.id,
            "jobType": self.job_type,
            "status": self.status,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
            "progress": self.progress,
            "total": self.total,
            "result": self.result,
            "error": self.error,
        }


class JobStore:
    """An in-memory job registry guarded by a lock."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, job_type: str) -> Job:
        """Register a new pending job."""
        job = Job(id=uuid.uuid4().hex, job_type=job_type)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job:
        """Return a job, raising when it does not exist."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError("job not found", details={"jobId": job_id})
        return job

    def list(self, *, limit: int = 50) -> list[Job]:
        """Return the most recent jobs."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)
        return jobs[:limit]

    def update(self, job_id: str, **changes: Any) -> Job:
        """Update the mutable fields of a job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise NotFoundError("job not found", details={"jobId": job_id})
            for key, value in changes.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            job.updated_at = _now()
            return job

    def run_async(self, job_id: str, work: Callable[[Job], dict[str, Any]]) -> None:
        """Run the work in a background thread and record the outcome."""

        def _runner() -> None:
            job = self.get(job_id)
            self.update(job_id, status="running")
            try:
                result = work(job)
                self.update(job_id, status="succeeded", result=result)
            except Exception as exc:  # the failure is recorded, not raised
                logger.exception("job %s failed", job_id)
                self.update(job_id, status="failed", error=str(exc))

        thread = threading.Thread(target=_runner, name=f"job-{job_id}", daemon=True)
        thread.start()


class RedisJobStore(JobStore):
    """A job store backed by Redis, for multi-worker deployments.

    Job state is written to Redis on every update, so any worker can serve
    the polling request. The in-memory copy is kept as a local cache.
    """

    def __init__(self, redis_client: Any, *, key_prefix: str = "fin-kg:job") -> None:
        super().__init__()
        self._redis = redis_client
        self._prefix = key_prefix

    def _key(self, job_id: str) -> str:
        return f"{self._prefix}:{job_id}"

    def create(self, job_type: str) -> Job:
        job = super().create(job_type)
        self._write(job)
        return job

    def get(self, job_id: str) -> Job:
        payload = self._redis.get(self._key(job_id))
        if payload:
            import json

            data = json.loads(payload)
            return Job(
                id=data["id"],
                job_type=data["job_type"],
                status=data["status"],
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                progress=data["progress"],
                total=data["total"],
                result=data.get("result"),
                error=data.get("error"),
            )
        return super().get(job_id)

    def update(self, job_id: str, **changes: Any) -> Job:
        job = super().update(job_id, **changes)
        self._write(job)
        return job

    def _write(self, job: Job) -> None:
        import json

        payload = {
            "id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "progress": job.progress,
            "total": job.total,
            "result": job.result,
            "error": job.error,
        }
        self._redis.set(self._key(job.id), json.dumps(payload, ensure_ascii=False))


__all__ = ["Job", "JobStore", "RedisJobStore"]

"""Periodic job orchestration.

The scheduler runs the recurring work of the platform:

``ingest``     pull the latest records from the configured sources
``rescore``    recompute the risk score of every entity
``propagate``  recompute the risk propagation and emit the new alerts
``research``   refresh the research material for the highest-risk entities

The implementation uses a built-in five-field cron matcher, evaluated on a
one-minute tick in a background thread. There is no optional scheduling
dependency: the removed ``schedule`` branch passed a cron expression to an
API that only accepts a time of day, so it could never fire correctly.

Every job run is measured and reported through the Prometheus metrics, and
failures are logged with the full traceback but never crash the scheduler.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fin_kg.core.logging import get_logger
from fin_kg.observability.metrics import JOB_DURATION, JOB_RUNS

logger = get_logger(__name__)


@dataclass(slots=True)
class JobDefinition:
    """One scheduled job."""

    name: str
    cron: str
    handler: Callable[[], Any]
    description: str = ""
    enabled: bool = True
    last_run: str | None = None
    last_status: str = "never"
    run_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cron": self.cron,
            "description": self.description,
            "enabled": self.enabled,
            "lastRun": self.last_run,
            "lastStatus": self.last_status,
            "runCount": self.run_count,
        }


class Scheduler:
    """Runs the registered jobs on their cron expressions."""

    def __init__(self, *, timezone_name: str = "Asia/Shanghai") -> None:
        self.timezone_name = timezone_name
        self.jobs: list[JobDefinition] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def register(self, job: JobDefinition) -> None:
        """Register a job."""
        with self._lock:
            self.jobs.append(job)
        logger.info("registered job %s with cron '%s'", job.name, job.cron)

    def run_job(self, job: JobDefinition) -> dict[str, Any]:
        """Execute one job, measuring it and recording the outcome."""
        started = time.monotonic()
        job.last_run = datetime.now(timezone.utc).isoformat()
        try:
            result = job.handler()
        except Exception as exc:  # a failing job must not stop the scheduler
            job.last_status = "failed"
            job.run_count += 1
            JOB_RUNS.labels(job_type=job.name, status="failed").inc()
            logger.exception("scheduled job %s failed", job.name)
            return {"job": job.name, "status": "failed", "error": str(exc)}
        duration = time.monotonic() - started
        job.last_status = "succeeded"
        job.run_count += 1
        JOB_RUNS.labels(job_type=job.name, status="succeeded").inc()
        JOB_DURATION.labels(job_type=job.name).observe(duration)
        logger.info("scheduled job %s finished in %.2fs", job.name, duration)
        return {"job": job.name, "status": "succeeded", "duration": duration, "result": result}

    def run_all_once(self) -> list[dict[str, Any]]:
        """Run every enabled job once. Used by the CLI and the tests."""
        with self._lock:
            jobs = list(self.jobs)
        return [self.run_job(job) for job in jobs if job.enabled]

    def status(self) -> list[dict[str, Any]]:
        """Return the state of every registered job."""
        with self._lock:
            jobs = list(self.jobs)
        return [job.as_dict() for job in jobs]

    # -- background execution ----------------------------------------------

    def _loop(self) -> None:
        """Evaluate the registered cron expressions on a one-minute tick.

        The built-in five-field matcher is the single code path. The former
        optional ``schedule`` branch could not work — it passed a five-field
        expression where the library expects a time of day — and any
        environment that happened to provide the module silently took that
        broken path. Expressions are interpreted in UTC; the configured
        timezone name is informational only (see ``docs/deployment.md`` and
        the pending R7-B04 decision).
        """
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            with self._lock:
                jobs = list(self.jobs)
            for job in jobs:
                if job.enabled and _cron_matches(job.cron, now):
                    threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
            self._stop.wait(60)

    def start(self) -> None:
        """Start the scheduler in a background thread."""
        if self._thread is not None:
            logger.warning("the scheduler is already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="fin-kg-scheduler", daemon=True)
        self._thread.start()
        logger.info("scheduler started with %s jobs", len(self.jobs))

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the scheduler and wait for the thread to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("scheduler stopped")


def _cron_matches(expression: str, moment: datetime) -> bool:
    """A minimal five-field cron matcher.

    Supports ``minute hour day month weekday`` with ``*``, a number, a list
    and a ``*/step`` step. It is not a full cron implementation, but it
    covers the expressions the configuration file uses.
    """
    fields = expression.split()
    if len(fields) != 5:
        return False
    _minute, _hour, _day, _month, _weekday = fields
    values = (moment.minute, moment.hour, moment.day, moment.month, moment.weekday() + 1)
    return all(_field_matches(spec, value) for spec, value in zip(fields, values, strict=False))


def _field_matches(spec: str, value: int) -> bool:
    """Whether one cron field matches the given value."""
    for part in spec.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                continue
            if step > 0 and value % step == 0:
                return True
            continue
        if "-" in part:
            start_text, _, end_text = part.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                continue
            if start <= value <= end:
                return True
            continue
        try:
            if int(part) == value:
                return True
        except ValueError:
            continue
    return False


def build_scheduler(
    settings: Any,
    *,
    handlers: dict[str, Callable[[], Any]],
) -> Scheduler:
    """Build a scheduler with the jobs described by the settings."""
    scheduler = Scheduler(timezone_name=settings.scheduler.timezone)
    definitions = (
        ("ingest", settings.scheduler.job_ingest_cron, "拉取最新数据并入库"),
        ("rescore", settings.scheduler.job_rescore_cron, "重算全图风险分"),
        ("propagate", settings.scheduler.job_propagate_cron, "重算风险传导并推送预警"),
        ("research", settings.scheduler.job_research_cron, "刷新高风险主体的投研材料"),
    )
    for name, cron, description in definitions:
        handler = handlers.get(name)
        if handler is None:
            continue
        scheduler.register(
            JobDefinition(name=name, cron=cron, handler=handler, description=description)
        )
    return scheduler


__all__ = ["JobDefinition", "Scheduler", "_cron_matches", "build_scheduler"]

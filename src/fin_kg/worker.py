"""The background worker process.

The worker runs the scheduled jobs in a separate process from the API, so
that a long ingestion never competes with request handling for the GIL or
for the database connection pool.

It is a separate entry point (``python -m fin_kg.worker``) rather than a
thread inside the API, because the API scales horizontally while the
scheduler must be a singleton — two schedulers would run every job twice.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import date, timedelta
from typing import Any

from fin_kg.core.logging import configure_logging, get_logger, register_secret_filter
from fin_kg.scheduler.runner import build_scheduler

logger = get_logger(__name__)


def build_handlers() -> dict[str, Any]:
    """Build the handlers of the scheduled jobs."""
    from fin_kg.api.deps import get_repository, get_risk_service, get_source_adapters
    from fin_kg.ingestion.pipeline import IngestionPipeline
    from fin_kg.observability.alerting import AlertDispatcher

    def ingest() -> dict[str, Any]:
        settings = __import__("fin_kg.config.settings", fromlist=["get_settings"]).get_settings()
        adapters = get_source_adapters()
        source = adapters.get("csv") or next(iter(adapters.values()))
        pipeline = IngestionPipeline(
            get_repository(),
            batch_size=settings.ingestion.batch_size,
            checkpoint_dir=settings.resolve_path(settings.ingestion.checkpoint_dir),
        )
        report = pipeline.run([source], since=date.today() - timedelta(days=1))
        return report.as_dict()

    def rescore() -> dict[str, Any]:
        service = get_risk_service()
        scores = service.rank(limit=200)
        return {"rescored": len(scores)}

    def propagate() -> dict[str, Any]:
        from fin_kg.config.settings import get_settings

        settings = get_settings()
        service = get_risk_service()
        dispatcher = AlertDispatcher.from_settings(settings)
        alerts = service.alerts(min_severity="warning", limit=200)
        counts = dispatcher.dispatch_many(alerts)
        return {"alerts": len(alerts), **counts}

    def research() -> dict[str, Any]:
        from fin_kg.api.deps import get_report_generator

        service = get_risk_service()
        generator = get_report_generator()
        scores = service.rank(limit=20)
        produced = []
        for score in scores:
            report = generator.generate(service.assess(score.entity_id))
            produced.append(report.entity_id)
        return {"reports": len(produced)}

    return {
        "ingest": ingest,
        "rescore": rescore,
        "propagate": propagate,
        "research": research,
    }


def main() -> int:
    """Run the worker until it is asked to stop."""
    from fin_kg.config.settings import get_settings

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    register_secret_filter(
        settings.neo4j.password.get_secret_value(),
        settings.redis.password.get_secret_value(),
        settings.llm.api_key.get_secret_value(),
    )

    scheduler = build_scheduler(settings, handlers=build_handlers())
    if not scheduler.jobs:
        logger.error("no jobs are configured — nothing to do")
        return 1

    scheduler.start()
    logger.info("worker started with %s jobs", len(scheduler.jobs))

    stopping = {"flag": False}

    def _handle(signum: int, _frame: Any) -> None:
        logger.info("received signal %s, shutting down", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    try:
        while not stopping["flag"]:
            time.sleep(1)
    finally:
        scheduler.stop()
    logger.info("worker stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

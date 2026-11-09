"""Health, readiness and metrics endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, Response

from fin_kg.api.deps import Repository, SettingsDep
from fin_kg.api.schemas import HealthResponse
from fin_kg.observability.metrics import (
    DEPENDENCY_UP,
    GRAPH_NODES,
    GRAPH_RELATIONSHIPS,
    render_metrics,
)

router = APIRouter(tags=["ops"])

#: The application start time, used to report the uptime.
_STARTED_AT = time.monotonic()


@router.get("/health", response_model=HealthResponse, summary="存活与依赖健康检查")
def health(repository: Repository, settings: SettingsDep) -> HealthResponse:
    """Report the liveness of the service and the state of its dependencies."""
    from fin_kg import __version__

    dependencies: dict[str, str] = {}
    try:
        stats = repository.stats()
        dependencies["graph_store"] = "ok"
        dependencies["nodeCount"] = str(
            sum(count for key, count in stats.items() if not key.startswith("REL:"))
        )
        DEPENDENCY_UP.labels(name="graph_store").set(1)
        # Publish the graph size. Without this the gauges stay at zero and
        # an operator cannot tell an empty graph from an unwired exporter.
        for key, count in stats.items():
            if key.startswith("REL:"):
                GRAPH_RELATIONSHIPS.labels(type=key.removeprefix("REL:")).set(count)
            else:
                GRAPH_NODES.labels(label=key).set(count)
    except Exception as exc:  # reported, not raised
        dependencies["graph_store"] = f"error: {type(exc).__name__}"
        DEPENDENCY_UP.labels(name="graph_store").set(0)

    try:
        repository.get_node("__healthcheck__")
        dependencies["graph_query"] = "ok"
        DEPENDENCY_UP.labels(name="graph_query").set(1)
    except Exception as exc:
        dependencies["graph_query"] = f"error: {type(exc).__name__}"
        DEPENDENCY_UP.labels(name="graph_query").set(0)

    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.env,
        uptimeSeconds=round(time.monotonic() - _STARTED_AT, 3),
        dependencies=dependencies,
    )


@router.get("/ready", summary="就绪检查")
def ready(repository: Repository) -> dict[str, str]:
    """Return 200 when the service can serve traffic."""
    repository.get_node("__ready__")
    return {"status": "ready"}


@router.get("/metrics", summary="Prometheus 指标", response_class=Response)
def metrics() -> Response:
    """Expose the Prometheus metrics of the process."""
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")


__all__ = ["router"]

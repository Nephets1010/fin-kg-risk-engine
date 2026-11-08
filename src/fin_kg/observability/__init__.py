"""Observability: Prometheus metrics and alert dispatch."""

from fin_kg.observability.alerting import (
    AlertChannel,
    AlertDispatcher,
    ConsoleChannel,
    FileChannel,
    WebhookChannel,
)
from fin_kg.observability.metrics import (
    ALERTS,
    DEPENDENCY_UP,
    GRAPH_NODES,
    GRAPH_RELATIONSHIPS,
    HTTP_LATENCY,
    HTTP_REQUESTS,
    INGESTION_DURATION,
    INGESTION_RECORDS,
    JOB_DURATION,
    JOB_RUNS,
    PROPAGATED,
    REGISTRY,
    RISK_SCORES,
    render_metrics,
)

__all__ = [
    "ALERTS",
    "DEPENDENCY_UP",
    "GRAPH_NODES",
    "GRAPH_RELATIONSHIPS",
    "HTTP_LATENCY",
    "HTTP_REQUESTS",
    "INGESTION_DURATION",
    "INGESTION_RECORDS",
    "JOB_DURATION",
    "JOB_RUNS",
    "PROPAGATED",
    "REGISTRY",
    "RISK_SCORES",
    "AlertChannel",
    "AlertDispatcher",
    "ConsoleChannel",
    "FileChannel",
    "WebhookChannel",
    "render_metrics",
]

"""Prometheus metrics.

The metric set is deliberately small and focused on the four questions an
operator actually asks:

* Is the API up and how fast is it? (``http_requests_total``, latency histogram)
* Is the ingestion pipeline keeping up? (``ingestion_records_total``, duration)
* Is the risk engine healthy? (``risk_score_computed_total``, alert counters)
* Are the dependencies reachable? (``dependency_up``)

Metric names are declared as constants so that a typo cannot silently create
a second, unused metric.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

#: The registry every metric of the platform is registered in.
REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "fin_kg_http_requests_total",
    "Total number of HTTP requests",
    ["method", "path", "status"],
    registry=REGISTRY,
)

HTTP_LATENCY = Histogram(
    "fin_kg_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    registry=REGISTRY,
)

INGESTION_RECORDS = Counter(
    "fin_kg_ingestion_records_total",
    "Records processed by the ingestion pipeline",
    ["source", "outcome"],
    registry=REGISTRY,
)

INGESTION_DURATION = Histogram(
    "fin_kg_ingestion_duration_seconds",
    "Duration of one ingestion run",
    ["source"],
    registry=REGISTRY,
)

RISK_SCORES = Counter(
    "fin_kg_risk_scores_computed_total",
    "Risk scores computed",
    ["model"],
    registry=REGISTRY,
)

ALERTS = Counter(
    "fin_kg_alerts_total",
    "Alerts emitted by the rule engine",
    ["rule", "severity"],
    registry=REGISTRY,
)

PROPAGATED = Counter(
    "fin_kg_risk_propagations_total",
    "Risk propagations performed",
    registry=REGISTRY,
)

GRAPH_NODES = Gauge(
    "fin_kg_graph_nodes",
    "Number of nodes in the graph",
    ["label"],
    registry=REGISTRY,
)

GRAPH_RELATIONSHIPS = Gauge(
    "fin_kg_graph_relationships",
    "Number of relationships in the graph",
    ["type"],
    registry=REGISTRY,
)

DEPENDENCY_UP = Gauge(
    "fin_kg_dependency_up",
    "1 when the dependency is reachable, 0 otherwise",
    ["name"],
    registry=REGISTRY,
)

JOB_RUNS = Counter(
    "fin_kg_jobs_total",
    "Background jobs executed",
    ["job_type", "status"],
    registry=REGISTRY,
)

JOB_DURATION = Histogram(
    "fin_kg_job_duration_seconds",
    "Duration of a background job",
    ["job_type"],
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    """Render the registry in the Prometheus text exposition format."""
    return generate_latest(REGISTRY)


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
    "render_metrics",
]

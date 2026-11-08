"""Enterprise-grade financial knowledge graph risk early-warning and research platform.

The package is organised in strict layers so that every module has a single
responsibility and dependencies only ever point inwards:

``config``      environment-driven settings, no business logic
``core``        logging, error taxonomy, shared value objects
``domain``      pure Pydantic models describing entities, relations, indicators
``ingestion``   source adapters, cleaning and the ETL pipeline
``graph``       graph schema, repository abstraction and Cypher query library
``risk``        indicator computation, rule engine, scoring, propagation, GNN
``research``    KGQA, similar-case retrieval and report summarisation
``api``         FastAPI application, routers and dependency wiring
``scheduler``   periodic job orchestration
``observability`` Prometheus metrics and alert dispatch
"""

__version__ = "0.1.0"
__all__ = ["__version__"]

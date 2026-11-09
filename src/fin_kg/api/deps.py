"""Dependency wiring.

The FastAPI application obtains its collaborators through these providers.
Each provider is cached for the lifetime of the app, so the Neo4j driver is
created once and shared.

Two application modes are supported:

``neo4j``    the production mode — the graph store is Neo4j
``inmemory`` the offline/demo mode — an in-memory graph seeded from the
sample data or the synthetic generator

The mode is chosen by :func:`build_repository`, which reads the settings. It
falls back to the in-memory store when the Neo4j driver cannot be created,
so the API is always startable — which matters for CI and for a first run.
"""

from __future__ import annotations

import weakref
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status

from fin_kg.api.jobs import JobStore
from fin_kg.config.settings import Settings, get_settings
from fin_kg.core.logging import get_logger
from fin_kg.graph.repository import (
    FileGraphRepository,
    GraphRepository,
    Neo4jGraphRepository,
)
from fin_kg.ingestion.base import SourceAdapter
from fin_kg.ingestion.sources import (
    CsvSourceAdapter,
    SyntheticSourceAdapter,
    TushareSourceAdapter,
)
from fin_kg.research.kgqa import KGQA
from fin_kg.research.report import ReportGenerator
from fin_kg.research.similarity import SimilarCaseFinder
from fin_kg.risk.engine import RuleEngine
from fin_kg.risk.gnn.service import GnnRiskService
from fin_kg.risk.service import RiskService, RiskServiceConfig

logger = get_logger(__name__)


def create_repository(settings: Any = None) -> GraphRepository:
    """Build the graph repository from the settings.

    Resolution order:

    1. **Neo4j** — the production store.
    2. **File-backed graph** — a JSON snapshot under
       ``data/local-graph.json``. Used when Neo4j is unreachable, so that
       the CLI and the API still work end to end without a database.
    3. **In-memory graph** — the last resort, for the test suite.

    In production only Neo4j is acceptable: a missing graph store is a
    deployment error and must be visible immediately rather than silently
    serving an empty graph.
    """
    resolved = settings or get_settings()
    repository = None
    try:
        repository = Neo4jGraphRepository.from_settings(resolved)
        repository.ensure_schema()
        logger.info("graph store: Neo4j at %s", resolved.neo4j.uri)
        return repository
    except Exception as exc:  # the API must stay startable in dev
        if repository is not None:
            # Release the driver before falling back, otherwise its finaliser
            # complains at interpreter shutdown.
            try:
                repository.close()
            except Exception:  # best effort cleanup
                logger.debug("failed to close the Neo4j driver", exc_info=True)
        if resolved.env == "production":
            logger.error("Neo4j is unavailable in production: %s", exc)
            raise
        logger.warning("Neo4j is unavailable (%s); falling back to the local file graph", exc)
        local = FileGraphRepository(
            resolved.resolve_path(Path("data/local-graph.json")), autosave=True
        )
        local.ensure_schema()
        return local


@lru_cache(maxsize=1)
def get_repository() -> GraphRepository:
    """Return the process-wide graph repository."""
    return create_repository()


#: Per-repository service instances, so that a repository swap (in tests, or
#: after a reconnect) never serves a service bound to the previous store.
#:
#: The key is the repository object itself rather than ``id(repository)``:
#: CPython reuses the memory address of a freed object, so an id-keyed cache
#: can hand a caller the services of a *different* repository that happened
#: to be allocated at the same address.
_SERVICES: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()


def _service_for(repository: GraphRepository, name: str, factory: Any) -> Any:
    """Return the memoised service instance for a repository."""
    bucket = _SERVICES.setdefault(repository, {})
    if name not in bucket:
        bucket[name] = factory()
    return bucket[name]


@lru_cache(maxsize=1)
def get_rules() -> RuleEngine:
    """Return the rule engine loaded from the rule file."""
    settings = get_settings()
    path = settings.resolve_path(settings.risk.rules_path)
    try:
        return RuleEngine.from_yaml(path)
    except Exception as exc:  # a broken rule file must not kill the API
        logger.error("failed to load risk rules from %s: %s", path, exc)
        return RuleEngine(rules=[])


def get_gnn_service(repository: GraphRepository = Depends(get_repository)) -> GnnRiskService:
    """Return the GNN risk service bound to the active repository.

    The service is created without a trained model; loading one is an
    explicit operation (``fin-kg gnn load``), because a model file is a
    deployment artefact rather than something to guess at startup.
    """
    return _service_for(
        repository,
        "gnn_service",
        lambda: GnnRiskService(repository=repository),
    )


def get_risk_service(repository: GraphRepository = Depends(get_repository)) -> RiskService:
    """Return the risk service bound to the active repository."""
    return _service_for(
        repository,
        "risk_service",
        lambda: RiskService(
            repository=repository,
            rules=get_rules(),
            indicator_engine=_build_indicator_engine(repository),
            gnn=get_gnn_service(repository),
            config=RiskServiceConfig(
                alert_threshold=get_settings().risk.alert_threshold,
                critical_threshold=get_settings().risk.critical_threshold,
                max_hops=get_settings().risk.max_hops,
                decay=get_settings().risk.decay,
            ),
        ),
    )


def _build_indicator_engine(repository: GraphRepository) -> Any:
    from fin_kg.risk.indicators_engine import GraphIndicatorEngine, LocalIndicatorEngine

    if isinstance(repository, Neo4jGraphRepository):
        return GraphIndicatorEngine(repository)
    return LocalIndicatorEngine(repository)


@lru_cache(maxsize=1)
def get_job_store() -> JobStore:
    """Return the job registry."""
    return JobStore()


def get_kgqa(repository: GraphRepository = Depends(get_repository)) -> KGQA:
    """Return the KGQA engine bound to the active repository."""
    from fin_kg.research.llm import build_client

    return _service_for(
        repository,
        "kgqa",
        lambda: KGQA(
            repository,
            planner=None,
            llm=build_client(get_settings().llm),
        ),
    )


def get_report_generator(
    repository: GraphRepository = Depends(get_repository),
) -> ReportGenerator:
    """Return the research report generator bound to the active repository."""
    return _service_for(
        repository,
        "report_generator",
        lambda: ReportGenerator(repository, similar_cases=SimilarCaseFinder(repository)),
    )


def get_source_adapters() -> dict[str, SourceAdapter]:
    """Return the available source adapters, keyed by name."""
    settings = get_settings()
    adapters: dict[str, SourceAdapter] = {
        "synthetic": SyntheticSourceAdapter(),
        "csv": CsvSourceAdapter(settings.resolve_path(settings.ingestion.data_dir)),
    }
    token = settings.tushare.token.get_secret_value()
    if token:
        adapters["tushare"] = TushareSourceAdapter(token=token, base_url=settings.tushare.base_url)
    return adapters


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Reject the request when an API key is configured but not supplied."""
    settings = get_settings()
    expected = settings.api_key.get_secret_value()
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "invalid or missing API key"},
        )


Repository = Annotated[GraphRepository, Depends(get_repository)]
RiskServiceDep = Annotated[RiskService, Depends(get_risk_service)]
RulesDep = Annotated[RuleEngine, Depends(get_rules)]
KGQADep = Annotated[KGQA, Depends(get_kgqa)]
ReportDep = Annotated[ReportGenerator, Depends(get_report_generator)]
JobStoreDep = Annotated[JobStore, Depends(get_job_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def reset_dependencies() -> None:
    """Clear the provider caches. Used by tests."""
    for provider in (
        get_repository,
        get_rules,
        get_job_store,
    ):
        provider.cache_clear()
    _SERVICES.clear()


__all__ = [
    "JobStoreDep",
    "KGQADep",
    "ReportDep",
    "Repository",
    "RiskServiceDep",
    "RulesDep",
    "SettingsDep",
    "get_job_store",
    "get_kgqa",
    "get_report_generator",
    "get_repository",
    "get_risk_service",
    "get_rules",
    "get_settings",
    "get_source_adapters",
    "require_api_key",
    "reset_dependencies",
]

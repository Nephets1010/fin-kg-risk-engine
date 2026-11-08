"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = PROJECT_ROOT / "rules" / "risk_rules.yaml"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch):
    """Keep the tests independent of the developer's local ``.env``."""
    for key in list(os.environ):
        if key.startswith("FIN_KG_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FIN_KG_ENV", "test")
    monkeypatch.setenv("FIN_KG_LOG_LEVEL", "WARNING")

    from fin_kg.config.settings import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def rules_path() -> Path:
    """Path of the production rule file."""
    return RULES_PATH


@pytest.fixture
def repository():
    """An empty in-memory graph."""
    from fin_kg.graph.repository import InMemoryGraphRepository

    repo = InMemoryGraphRepository()
    repo.ensure_schema()
    return repo


@pytest.fixture
def seeded_repository(repository):
    """An in-memory graph seeded with the synthetic dataset."""
    from fin_kg.ingestion.pipeline import IngestionPipeline
    from fin_kg.ingestion.sources import SyntheticSourceAdapter

    pipeline = IngestionPipeline(repository)
    pipeline.run([SyntheticSourceAdapter(companies=30, events_per_company=2.0, seed=7)])
    return repository


@pytest.fixture
def rule_engine(rules_path: Path):
    """The production rule engine."""
    from fin_kg.risk.engine import RuleEngine

    return RuleEngine.from_yaml(rules_path)


@pytest.fixture
def risk_service(seeded_repository, rule_engine):
    """A risk service over the seeded graph."""
    from fin_kg.risk.service import build_service

    return build_service(seeded_repository, rule_engine)


@pytest.fixture
def app_with_repository():
    """Build a FastAPI app whose repository is an in-memory graph.

    The dependency is overridden through ``dependency_overrides`` so that no
    test can accidentally reach a real Neo4j instance.
    """
    from fastapi.testclient import TestClient

    from fin_kg.api import deps
    from fin_kg.api.app import create_app
    from fin_kg.graph.repository import InMemoryGraphRepository

    repo = InMemoryGraphRepository()
    repo.ensure_schema()

    application = create_app()
    application.dependency_overrides[deps.get_repository] = lambda: repo
    application.dependency_overrides[deps.get_rules] = deps.get_rules
    with TestClient(application) as test_client:
        test_client.repository = repo  # type: ignore[attr-defined]
        yield test_client
    application.dependency_overrides.clear()
    deps.reset_dependencies()


@pytest.fixture
def client(app_with_repository):
    """A FastAPI test client backed by an empty in-memory graph."""
    return app_with_repository

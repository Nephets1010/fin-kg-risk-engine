"""Regression tests for the defects fixed in the third audit round.

Each test here guards a contract that was previously broken. The three
defects share a theme: the implementation looked correct but was never
exercised by the path that matters.

- ``S-01``: the API key dependency existed but was never mounted, so a
  deployment that configured ``FIN_KG_API_KEY`` served an open API.
- ``S-03``: the in-memory store appended edges instead of merging them, so
  replaying an import duplicated the relationship graph.
- ``P-01``: the job registry grew without bound.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from fin_kg.api.jobs import JobStore
from fin_kg.graph.repository import InMemoryGraphRepository
from fin_kg.ingestion.pipeline import IngestionPipeline
from fin_kg.ingestion.sources import SyntheticSourceAdapter
from fin_kg.risk.engine import RuleEngine
from fin_kg.risk.service import build_service


class TestApiKeyEnforcement:
    """S-01 — the configured API key must actually gate the API."""

    @pytest.fixture
    def protected_client(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from fin_kg.api.app import create_app
        from fin_kg.config.settings import reset_settings_cache

        monkeypatch.setenv("FIN_KG_API_KEY", "audit-test-key")
        reset_settings_cache()
        try:
            yield TestClient(create_app())
        finally:
            monkeypatch.delenv("FIN_KG_API_KEY", raising=False)
            reset_settings_cache()

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/v1/graph/stats"),
            ("get", "/api/v1/risk/rank"),
            ("post", "/api/v1/research/ask"),
            ("get", "/api/v1/jobs"),
        ],
    )
    def test_business_endpoints_reject_a_missing_key(self, protected_client, method, path) -> None:
        if method == "post":
            response = protected_client.post(path, json={"question": "风险最高的公司"})
        else:
            response = protected_client.get(path)
        assert response.status_code == 401, f"{path} 未拦截无 Key 请求"

    def test_the_error_uses_the_flat_envelope(self, protected_client) -> None:
        response = protected_client.get("/api/v1/graph/stats")
        body = response.json()
        assert body["code"] == "unauthorized"
        assert "message" in body

    def test_a_wrong_key_is_rejected(self, protected_client) -> None:
        response = protected_client.get("/api/v1/graph/stats", headers={"X-API-Key": "wrong-key"})
        assert response.status_code == 401

    def test_the_correct_key_is_accepted(self, protected_client) -> None:
        response = protected_client.get(
            "/api/v1/graph/stats", headers={"X-API-Key": "audit-test-key"}
        )
        assert response.status_code == 200

    @pytest.mark.parametrize("path", ["/", "/health", "/ready", "/metrics"])
    def test_operational_endpoints_stay_open(self, protected_client, path) -> None:
        """Probes must keep working without a secret.

        A load balancer or a container orchestrator polls the health
        endpoint; requiring a secret there would make the service
        undeployable.
        """
        response = protected_client.get(path)
        assert response.status_code == 200, f"{path} 不应对运维探针要求鉴权"


class TestApiKeyNotConfigured:
    """Without a configured key the API must stay open — no behaviour change."""

    def test_endpoints_are_reachable(self, client: TestClient) -> None:
        assert client.get("/api/v1/graph/stats").status_code == 200


class TestIngestionIdempotency:
    """S-03 — replaying an import must not duplicate the graph."""

    def test_a_repeated_import_does_not_duplicate_edges(self) -> None:
        repository = InMemoryGraphRepository()
        repository.ensure_schema()
        pipeline = IngestionPipeline(repository)
        source = SyntheticSourceAdapter(companies=20, events_per_company=2.0, seed=5)

        pipeline.run([source])
        first = repository.stats()
        pipeline.run([source])
        second = repository.stats()

        assert first == second, "重复导入后图谱规模发生变化"

    def test_a_repeated_import_keeps_the_same_scores(self) -> None:
        repository = InMemoryGraphRepository()
        repository.ensure_schema()
        pipeline = IngestionPipeline(repository)
        source = SyntheticSourceAdapter(companies=20, events_per_company=2.0, seed=5)
        rules = RuleEngine.from_yaml("rules/risk_rules.yaml")

        pipeline.run([source])
        first = build_service(repository, rules).rank(limit=1)[0].score
        pipeline.run([source])
        second = build_service(repository, rules).rank(limit=1)[0].score

        assert first == pytest.approx(second)

    def test_the_merge_key_is_source_target_and_type(self) -> None:
        """Two relations of different types between the same pair coexist."""
        from fin_kg.domain.entities import Company
        from fin_kg.domain.relations import Relation, RelationType

        repository = InMemoryGraphRepository()
        repository.ensure_schema()
        repository.upsert_nodes([Company(id="a", name="甲"), Company(id="b", name="乙")])
        repository.upsert_relations(
            [
                Relation(
                    source_id="a",
                    target_id="b",
                    relation_type=RelationType.HOLD,
                    properties={"percent": 0.5},
                )
            ]
        )
        repository.upsert_relations(
            [
                Relation(
                    source_id="a",
                    target_id="b",
                    relation_type=RelationType.GUARANTEE,
                    properties={"guaranteed_amount": 100},
                )
            ]
        )
        edges = [
            edge
            for edge in repository.snapshot.edges
            if edge["source"] == "a" and edge["target"] == "b"
        ]
        assert len(edges) == 2, "不同类型的关系被错误合并"

    def test_a_repeated_relation_updates_the_properties(self) -> None:
        from fin_kg.domain.entities import Company
        from fin_kg.domain.relations import Relation, RelationType

        repository = InMemoryGraphRepository()
        repository.ensure_schema()
        repository.upsert_nodes([Company(id="a", name="甲"), Company(id="b", name="乙")])
        for percent in (0.3, 0.6):
            repository.upsert_relations(
                [
                    Relation(
                        source_id="a",
                        target_id="b",
                        relation_type=RelationType.HOLD,
                        properties={"percent": percent},
                    )
                ]
            )
        edges = [
            edge
            for edge in repository.snapshot.edges
            if edge["source"] == "a" and edge["target"] == "b"
        ]
        assert len(edges) == 1
        assert edges[0]["percent"] == pytest.approx(0.6)


class TestJobStoreBounds:
    """P-01 — the job registry must not grow without bound."""

    def test_the_registry_is_capped(self) -> None:
        store = JobStore(max_jobs=10)
        for _ in range(100):
            store.create("ingest")
        assert len(store._jobs) <= 10

    def test_a_running_job_survives_the_ttl(self) -> None:
        store = JobStore(max_jobs=100, ttl_seconds=1)
        job = store.create("ingest")
        store.update(job.id, status="running")
        time.sleep(1.2)
        store.create("ingest")
        assert store.get(job.id) is not None, "运行中的任务被 TTL 淘汰"

    def test_a_finished_job_is_evicted_after_the_ttl(self) -> None:
        store = JobStore(max_jobs=100, ttl_seconds=1)
        job = store.create("ingest")
        store.update(job.id, status="succeeded")
        time.sleep(1.2)
        store.create("ingest")
        assert job.id not in store._jobs, "已完成的任务未被淘汰"

    def test_the_oldest_finished_job_is_evicted_first(self) -> None:
        store = JobStore(max_jobs=2)
        first = store.create("ingest")
        store.update(first.id, status="succeeded")
        second = store.create("ingest")
        store.update(second.id, status="succeeded")
        third = store.create("ingest")
        store.update(third.id, status="succeeded")
        assert first.id not in store._jobs
        assert second.id in store._jobs
        assert third.id in store._jobs

    def test_the_default_limit_is_bounded(self) -> None:
        store = JobStore()
        assert store._max_jobs > 0
        assert store._max_jobs <= 10_000


class TestLocalIndicatorEngineVisibility:
    """B-01 — an unimplemented indicator must be visible, not silent."""

    def test_an_unimplemented_indicator_is_logged(self, caplog) -> None:
        import logging
        from datetime import date

        from fin_kg.domain.indicators import (
            IndicatorCategory,
            IndicatorDirection,
            IndicatorSpec,
        )
        from fin_kg.risk.indicators_engine import LocalIndicatorEngine

        spec = IndicatorSpec(
            id="IND-NEW-999",
            name="新指标",
            category=IndicatorCategory.FINANCIAL,
            version="1.0.0",
            direction=IndicatorDirection.HIGHER_IS_RISKIER,
            weight=0.2,
            unit="次",
            description="一个本地引擎尚未实现的指标",
            measurement_cypher="RETURN 5 AS value",
            thresholds=((0, 0.0), (1, 100.0)),
        )
        repository = InMemoryGraphRepository()
        repository.ensure_schema()
        engine = LocalIndicatorEngine(repository)

        # The platform logger does not propagate to the root logger, so
        # pytest's handler is attached to the fin_kg tree directly.
        platform_logger = logging.getLogger("fin_kg")
        previous = platform_logger.propagate
        platform_logger.propagate = True
        try:
            with caplog.at_level(logging.WARNING):
                value = engine.measure(spec, "company-x", since=date(2026, 1, 1))
        finally:
            platform_logger.propagate = previous

        assert value is None, "行为契约：未实现的指标仍返回 None"
        assert any("IND-NEW-999" in record.getMessage() for record in caplog.records), (
            "未实现的指标必须留下告警，否则会被误读为「无数据」"
        )

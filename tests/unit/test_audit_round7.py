"""Cross-cutting regression checks for the seventh engineering round.

The individual fixes are covered in their own test modules. This file holds
the audit-level assertions: that the new observability signal really exists,
that a broken rule file is visible without becoming a startup failure, and
that the description-only change stayed description-only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fin_kg.api import deps
from fin_kg.core.errors import GraphStoreError
from fin_kg.domain.entities import Company
from fin_kg.domain.risk import RiskScore, TransmissionPath
from fin_kg.graph.repository import (
    ALLOWED_LABELS,
    VALID_NODE_LABELS,
    InMemoryGraphRepository,
    Neo4jGraphRepository,
)
from fin_kg.observability.metrics import REGISTRY, render_metrics
from fin_kg.risk.engine import RuleEngine


class TestRulesLoadedSignal:
    """R7-A05: additive observability, no fail-fast behaviour change."""

    def test_gauge_is_registered_and_exposed(self) -> None:
        assert REGISTRY.get_sample_value("fin_kg_rules_loaded") is not None
        assert "fin_kg_rules_loaded" in render_metrics().decode("utf-8")

    def test_success_sets_the_loaded_rule_count(self, rule_engine) -> None:
        deps.reset_dependencies()
        engine = deps.get_rules()
        assert len(engine.rules) == len(rule_engine.rules)
        assert REGISTRY.get_sample_value("fin_kg_rules_loaded") == float(len(engine.rules))

    def test_broken_rule_file_sets_zero_but_still_returns_an_engine(
        self, tmp_path, monkeypatch
    ) -> None:
        broken = tmp_path / "broken.yaml"
        broken.write_text("rules: [ {invalid: ", encoding="utf-8")

        class _BrokenSettings:
            risk = SimpleNamespace(rules_path=Path("rules/risk_rules.yaml"))

            def resolve_path(self, relative):
                return broken

        monkeypatch.setattr(deps, "get_settings", lambda: _BrokenSettings())
        deps.reset_dependencies()
        engine = deps.get_rules()
        assert engine.rules == []
        assert REGISTRY.get_sample_value("fin_kg_rules_loaded") == 0.0

        # The API must still start and serve health checks with an empty
        # engine — only the metric tells the operator something is wrong.
        from fin_kg.api.app import create_app
        from fin_kg.graph.repository import InMemoryGraphRepository

        application = create_app()
        repository = InMemoryGraphRepository()
        repository.ensure_schema()
        application.dependency_overrides[deps.get_repository] = lambda: repository
        with TestClient(application) as client:
            assert client.get("/health").status_code == 200
        application.dependency_overrides.clear()
        deps.reset_dependencies()

    def test_client_fixture_still_sees_a_healthy_rule_count(self, client: TestClient) -> None:
        """The normal path is untouched by the new signal."""
        assert client.get("/health").status_code == 200


class TestDescriptionOnlyChange:
    """R7-A08: wording changed, measurement semantics did not."""

    def test_negative_news_description_matches_the_cypher_window(self) -> None:
        from fin_kg.domain.indicators import INDICATOR_BY_ID

        spec = INDICATOR_BY_ID["IND-NEG-001"]
        assert "近 12 个月" in spec.description
        assert "近 30 天" not in spec.description
        # The 365-day window and the thresholds are deliberately unchanged.
        assert "date($since)" in spec.measurement_cypher
        assert spec.thresholds == ((0, 0.0), (3, 40.0), (10, 70.0), (25, 100.0))
        assert spec.weight == 0.05


class TestFinalRound7Assertions:
    """The four highest-risk regressions, re-checked after all five tasks."""

    def test_a01_guards_hold_on_both_store_implementations(self, repository) -> None:
        assert VALID_NODE_LABELS is ALLOWED_LABELS
        malicious = "Company) DETACH DELETE n MATCH (m:Company"
        for store in (repository, Neo4jGraphRepository(object())):
            with pytest.raises(GraphStoreError):
                store.find_nodes(malicious)
            with pytest.raises(GraphStoreError):
                store.find_nodes("Company", order_by="riskScore; DETACH DELETE n")

    def test_a02_entity_key_is_the_unique_node_count(self) -> None:
        store = InMemoryGraphRepository()
        store.upsert_nodes([Company(id="a", name="甲"), Company(id="b", name="乙")])
        assert store.stats()["Entity"] == 2 == len(store.snapshot.nodes)

    def test_a05_signal_survives_the_full_run(self) -> None:
        deps.reset_dependencies()
        engine = deps.get_rules()
        assert engine.rules
        assert REGISTRY.get_sample_value("fin_kg_rules_loaded") == float(len(engine.rules))
        assert "fin_kg_rules_loaded" in render_metrics().decode("utf-8")

    def test_a07_no_duplicate_affected_entities(self) -> None:
        engine = RuleEngine.from_payload(
            {
                "rules": [
                    {
                        "id": "R-FINAL",
                        "name": "path",
                        "severity": "warning",
                        "condition": {
                            "all": [
                                {
                                    "path": {
                                        "relation_types": ["GUARANTEE"],
                                        "min_hops": 1,
                                        "min_paths": 1,
                                    }
                                }
                            ]
                        },
                        "message": "hit",
                    }
                ]
            }
        )
        path = TransmissionPath(
            node_ids=["a", "b", "c"],
            node_names=["A", "B", "C"],
            relation_types=["GUARANTEE", "GUARANTEE"],
            transmissibilities=[1.0, 1.0],
            propagated_score=40.0,
            hops=2,
        )
        assessment = engine.evaluate_assessment(RiskScore(entity_id="a", score=40), [path, path])
        ids = [item["id"] for item in assessment.affected_entities]
        assert ids == ["b", "c"] == list(dict.fromkeys(ids))
        assert assessment.alerts[0].affected_entity_ids == ["b", "c"]

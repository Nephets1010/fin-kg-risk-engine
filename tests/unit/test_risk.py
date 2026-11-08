"""Unit tests for the risk engine: scoring, rules, propagation and the GNN."""

from __future__ import annotations

import numpy as np
import pytest

from fin_kg.core.errors import ValidationError
from fin_kg.domain.entities import Company
from fin_kg.domain.relations import Relation, RelationType
from fin_kg.domain.risk import IndicatorReading, RiskScore, TransmissionPath
from fin_kg.risk.engine import RuleEngine, alert_fingerprint
from fin_kg.risk.gnn.features import FeatureExtractor, normalize_features
from fin_kg.risk.gnn.model import GraphSAGEConfig, NumpySAGE
from fin_kg.risk.gnn.trainer import LabelStore, precision_recall_f1, roc_auc
from fin_kg.risk.propagation import RiskPropagator
from fin_kg.risk.scoring import RiskScorer


class TestRiskScorer:
    def _reading(self, indicator_id: str, measurement: float | None, sub: float, weight: float):
        return IndicatorReading(
            indicator_id=indicator_id,
            indicator_name=indicator_id,
            version="1.0.0",
            category="legal",
            measurement=measurement,
            sub_score=sub,
            weight=weight,
        )

    def test_score_is_weighted_average(self) -> None:
        from fin_kg.risk.indicators_engine import IndicatorEngine

        class StubEngine(IndicatorEngine):
            def measure(self, spec, entity_id, *, since):
                return None

        scorer = RiskScorer(StubEngine())
        readings = [
            self._reading("A", 10.0, 80.0, 0.5),
            self._reading("B", 1.0, 20.0, 0.5),
        ]
        breakdown = scorer.breakdown(readings)
        assert breakdown.weighted_sum == pytest.approx(50.0)
        assert breakdown.weight_total == pytest.approx(1.0)
        assert breakdown.coverage == pytest.approx(1.0)

    def test_missing_readings_lower_coverage(self) -> None:
        from fin_kg.risk.indicators_engine import IndicatorEngine

        class StubEngine(IndicatorEngine):
            def measure(self, spec, entity_id, *, since):
                return None

        scorer = RiskScorer(StubEngine())
        readings = [
            self._reading("A", 10.0, 80.0, 0.5),
            self._reading("B", None, 0.0, 0.5),
        ]
        breakdown = scorer.breakdown(readings)
        assert breakdown.coverage == pytest.approx(0.5)
        assert breakdown.missing_indicators == ("B",)

    def test_score_is_bounded(self, seeded_repository) -> None:
        from fin_kg.risk.indicators_engine import LocalIndicatorEngine

        scorer = RiskScorer(LocalIndicatorEngine(seeded_repository))
        companies = seeded_repository.find_nodes("Company")
        if not companies:
            pytest.skip("no companies in the seeded graph")
        score = scorer.score(companies[0]["id"], entity_name=companies[0]["name"])
        assert 0.0 <= score.score <= 100.0


class TestRuleEngine:
    def test_production_rules_load(self, rule_engine) -> None:
        assert len(rule_engine.rules) >= 10
        assert all(rule.id for rule in rule_engine.rules)

    def test_rule_ids_are_unique(self, rule_engine) -> None:
        ids = [rule.id for rule in rule_engine.rules]
        assert len(ids) == len(set(ids))

    def test_every_rule_has_a_condition(self, rule_engine) -> None:
        for rule in rule_engine.rules:
            assert rule.clauses

    def test_indicator_clause_matches(self) -> None:
        engine = RuleEngine.from_payload(
            {
                "rules": [
                    {
                        "id": "R-TEST",
                        "name": "test",
                        "severity": "warning",
                        "condition": {
                            "all": [{"indicator": "IND-LIT-001", "operator": "gte", "value": 3}]
                        },
                        "message": "hit",
                    }
                ]
            }
        )
        score = RiskScore(
            entity_id="c1",
            entity_name="测试",
            score=70,
            readings=[
                IndicatorReading(
                    indicator_id="IND-LIT-001",
                    indicator_name="涉诉事件数",
                    version="1.0.0",
                    category="legal",
                    measurement=5,
                    sub_score=80.0,
                    weight=0.15,
                )
            ],
        )
        alerts = engine.evaluate(score)
        assert len(alerts) == 1
        assert alerts[0].rule_id == "R-TEST"

    def test_indicator_clause_requires_data(self) -> None:
        engine = RuleEngine.from_payload(
            {
                "rules": [
                    {
                        "id": "R-TEST",
                        "name": "test",
                        "severity": "warning",
                        "condition": {
                            "all": [{"indicator": "IND-LIT-001", "operator": "gte", "value": 1}]
                        },
                        "message": "hit",
                    }
                ]
            }
        )
        score = RiskScore(entity_id="c1", score=10, readings=[])
        assert engine.evaluate(score) == []

    def test_score_clause(self) -> None:
        engine = RuleEngine.from_payload(
            {
                "rules": [
                    {
                        "id": "R-SCORE",
                        "name": "score",
                        "severity": "major",
                        "condition": {"all": [{"score": {"operator": "gte", "value": 60}}]},
                        "message": "hit",
                    }
                ]
            }
        )
        assert len(engine.evaluate(RiskScore(entity_id="c1", score=70))) == 1
        assert len(engine.evaluate(RiskScore(entity_id="c1", score=20))) == 0

    def test_path_clause(self) -> None:
        engine = RuleEngine.from_payload(
            {
                "rules": [
                    {
                        "id": "R-PATH",
                        "name": "path",
                        "severity": "critical",
                        "condition": {
                            "all": [
                                {
                                    "path": {
                                        "relation_types": ["GUARANTEE"],
                                        "min_hops": 2,
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
            propagated_score=50,
            hops=2,
        )
        assert len(engine.evaluate(RiskScore(entity_id="a", score=50), [path])) == 1

    def test_disabled_rule_does_not_fire(self) -> None:
        engine = RuleEngine.from_payload(
            {
                "rules": [
                    {
                        "id": "R-OFF",
                        "name": "off",
                        "severity": "warning",
                        "enabled": False,
                        "condition": {"all": [{"score": {"operator": "gte", "value": 1}}]},
                        "message": "hit",
                    }
                ]
            }
        )
        assert engine.evaluate(RiskScore(entity_id="c1", score=90)) == []

    def test_invalid_operator_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuleEngine.from_payload(
                {
                    "rules": [
                        {
                            "id": "R-BAD",
                            "name": "bad",
                            "severity": "warning",
                            "condition": {
                                "all": [{"indicator": "X", "operator": "matches", "value": 1}]
                            },
                            "message": "hit",
                        }
                    ]
                }
            )

    def test_rule_without_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuleEngine.from_payload(
                {"rules": [{"name": "x", "severity": "warning", "condition": {"all": []}}]}
            )

    def test_alert_fingerprint_is_stable(self) -> None:
        from fin_kg.domain.risk import AlertSeverity, RiskAlert

        alert = RiskAlert(
            alert_id="a",
            rule_id="R",
            rule_name="r",
            severity=AlertSeverity.WARNING,
            entity_id="c1",
            message="m",
        )
        assert alert_fingerprint(alert) == alert_fingerprint(alert)


class TestPropagation:
    def _graph(self):
        from fin_kg.graph.repository import InMemoryGraphRepository

        repo = InMemoryGraphRepository()
        repo.ensure_schema()
        repo.upsert_nodes(
            [
                Company(id="a", name="A"),
                Company(id="b", name="B"),
                Company(id="c", name="C"),
            ]
        )
        repo.upsert_relations(
            [
                Relation(
                    source_id="a",
                    target_id="b",
                    relation_type=RelationType.GUARANTEE,
                    properties={"guaranteed_amount": 100},
                ),
                Relation(
                    source_id="b",
                    target_id="c",
                    relation_type=RelationType.GUARANTEE,
                    properties={"guaranteed_amount": 100},
                ),
            ]
        )
        return repo

    def test_propagation_follows_the_chain(self) -> None:
        repo = self._graph()
        propagator = RiskPropagator(repo, max_hops=3, decay=0.6)
        score = RiskScore(entity_id="a", entity_name="A", score=100)
        result = propagator.propagate(score)
        assert {item["id"] for item in result.affected} == {"b", "c"}

    def test_decay_reduces_the_score(self) -> None:
        repo = self._graph()
        propagator = RiskPropagator(repo, max_hops=3, decay=0.6)
        score = RiskScore(entity_id="a", entity_name="A", score=100)
        result = propagator.propagate(score)
        by_id = {item["id"]: item["score"] for item in result.affected}
        assert by_id["b"] > by_id["c"]

    def test_max_hops_is_respected(self) -> None:
        repo = self._graph()
        propagator = RiskPropagator(repo, max_hops=1, decay=0.6)
        score = RiskScore(entity_id="a", entity_name="A", score=100)
        result = propagator.propagate(score)
        assert {item["id"] for item in result.affected} == {"b"}

    def test_zero_score_propagates_nothing(self) -> None:
        repo = self._graph()
        propagator = RiskPropagator(repo)
        result = propagator.propagate(RiskScore(entity_id="a", score=0))
        assert result.affected == []

    def test_path_text_is_readable(self) -> None:
        repo = self._graph()
        propagator = RiskPropagator(repo)
        score = RiskScore(entity_id="a", entity_name="A", score=100)
        result = propagator.propagate(score)
        assert "GUARANTEE" in result.paths[0].to_text()

    def test_bottleneck_is_the_weakest_link(self) -> None:
        path = TransmissionPath(
            node_ids=["a", "b"],
            node_names=["A", "B"],
            relation_types=["RELATED"],
            transmissibilities=[0.3],
            propagated_score=10,
            hops=1,
        )
        assert path.bottleneck == pytest.approx(0.3)


class TestGNN:
    def test_forward_pass_shape(self) -> None:
        model = NumpySAGE(GraphSAGEConfig(in_dim=4, hidden_dim=3, epochs=1))
        features = np.random.default_rng(0).normal(size=(6, 4))
        edge_index = np.array([[0, 1, 2], [1, 2, 3]], dtype=np.int64)
        logits, cache = model.forward(features, edge_index)
        assert logits.shape == (6, 1)
        assert cache["hidden"].shape == (6, 3)

    def test_training_reduces_loss(self) -> None:
        rng = np.random.default_rng(1)
        features = rng.normal(size=(40, 4))
        edge_index = np.array([list(range(39)), [i + 1 for i in range(39)]], dtype=np.int64)
        labels = np.array([1.0 if i % 4 == 0 else 0.0 for i in range(40)])
        model = NumpySAGE(GraphSAGEConfig(in_dim=4, hidden_dim=8, epochs=60, learning_rate=0.05))
        report = model.train(features, edge_index, labels)
        assert report["finalAccuracy"] >= 0.5

    def test_predict_proba_is_a_probability(self) -> None:
        model = NumpySAGE(GraphSAGEConfig(in_dim=3, hidden_dim=2, epochs=1))
        features = np.zeros((4, 3))
        edge_index = np.zeros((2, 0), dtype=np.int64)
        probabilities = model.predict_proba(features, edge_index)
        assert ((probabilities >= 0) & (probabilities <= 1)).all()

    def test_model_round_trip(self, tmp_path) -> None:
        model = NumpySAGE(GraphSAGEConfig(in_dim=3, hidden_dim=2, epochs=1))
        path = tmp_path / "model.json"
        model.save(path)
        restored = NumpySAGE.load(path)
        assert restored.w_self.shape == model.w_self.shape
        assert np.allclose(restored.w_out, model.w_out)

    def test_feature_extraction(self, seeded_repository) -> None:
        extractor = FeatureExtractor(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        if not companies:
            pytest.skip("no companies")
        subgraph = extractor.extract([companies[0]["id"]], max_hops=1)
        assert subgraph.num_nodes >= 1
        assert subgraph.features.shape[1] == 9

    def test_normalize_handles_constant_columns(self) -> None:
        features = np.ones((5, 3))
        normalized = normalize_features(features)
        assert not np.isnan(normalized).any()

    def test_label_store_round_trip(self, tmp_path) -> None:
        store = LabelStore()
        store.set("c1", 1)
        store.set("c2", 0)
        path = tmp_path / "labels.csv"
        store.save(path)
        restored = LabelStore.from_csv(path)
        assert restored.get("c1") == 1
        assert restored.get("c2") == 0

    def test_label_store_rejects_bad_header(self, tmp_path) -> None:
        path = tmp_path / "labels.csv"
        path.write_text("id,label\nc1,1\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            LabelStore.from_csv(path)

    def test_precision_recall_f1_perfect_prediction(self) -> None:
        labels = np.array([1.0, 1.0, 0.0, 0.0])
        predictions = np.array([0.9, 0.8, 0.1, 0.2])
        precision, recall, f1 = precision_recall_f1(predictions, labels)
        assert precision == pytest.approx(1.0)
        assert recall == pytest.approx(1.0)
        assert f1 == pytest.approx(1.0)

    def test_roc_auc_separated_classes(self) -> None:
        labels = np.array([0.0, 0.0, 1.0, 1.0])
        predictions = np.array([0.1, 0.2, 0.8, 0.9])
        assert roc_auc(predictions, labels) == pytest.approx(1.0)

    def test_roc_auc_without_positives_is_zero(self) -> None:
        assert roc_auc(np.array([0.1, 0.2]), np.array([0.0, 0.0])) == 0.0

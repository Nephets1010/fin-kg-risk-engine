"""Unit tests for the GNN risk service and the two-score contract.

ADR-0004 promises that the baseline score and the model score are produced
*in parallel* and never fused. These tests pin that promise: the baseline
must stay identical whether or not a model exists, and the model score must
appear only when a model is actually available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fin_kg.core.errors import ValidationError
from fin_kg.domain.risk import RiskScore
from fin_kg.risk.gnn.service import GnnRiskService, ModelScore
from fin_kg.risk.gnn.trainer import LabelStore
from fin_kg.risk.service import RiskServiceConfig, build_service


@pytest.fixture
def labelled_graph(seeded_repository):
    """A graph with enough labelled nodes to train on."""
    companies = seeded_repository.find_nodes("Company")
    if len(companies) < 12:
        pytest.skip("the seeded graph is too small to train on")
    labels = LabelStore()
    for index, company in enumerate(companies):
        labels.set(company["id"], 1 if index % 4 == 0 else 0)
    return seeded_repository, labels


class TestModelScore:
    def test_the_probability_is_expressed_on_the_risk_scale(self) -> None:
        score = ModelScore(entity_id="c1", probability=0.42)
        assert score.score == pytest.approx(42.0)

    def test_the_payload_is_serialisable(self) -> None:
        payload = ModelScore(entity_id="c1", probability=0.42).as_dict()
        assert payload["entityId"] == "c1"
        assert payload["score"] == pytest.approx(42.0)


class TestGnnRiskService:
    def test_a_new_service_has_no_model(self, seeded_repository) -> None:
        service = GnnRiskService(repository=seeded_repository)
        assert service.is_ready is False
        assert service.score("any") is None

    def test_training_makes_the_service_ready(self, labelled_graph) -> None:
        repository, labels = labelled_graph
        service = GnnRiskService(repository=repository)
        service.train(labels)
        assert service.is_ready is True

    def test_the_model_score_is_returned_after_training(self, labelled_graph) -> None:
        repository, labels = labelled_graph
        service = GnnRiskService(repository=repository)
        service.train(labels)
        companies = repository.find_nodes("Company")
        score = service.score(str(companies[0]["id"]))
        assert score is not None
        assert 0.0 <= score.probability <= 1.0

    def test_score_many_covers_every_requested_entity(self, labelled_graph) -> None:
        repository, labels = labelled_graph
        service = GnnRiskService(repository=repository)
        service.train(labels)
        companies = repository.find_nodes("Company")
        ids = [str(company["id"]) for company in companies[:5]]
        scores = service.score_many(ids)
        assert len(scores) == len(ids)

    def test_score_many_without_a_model_returns_nothing(self, seeded_repository) -> None:
        service = GnnRiskService(repository=seeded_repository)
        assert service.score_many(["a", "b"]) == []

    def test_the_model_round_trips_through_a_file(self, tmp_path: Path, labelled_graph) -> None:
        repository, labels = labelled_graph
        service = GnnRiskService(repository=repository)
        service.train(labels)

        path = tmp_path / "model.json"
        service.save(path)
        assert path.exists()

        reloaded = GnnRiskService(repository=repository)
        reloaded.load(path)
        assert reloaded.is_ready is True
        companies = repository.find_nodes("Company")
        assert reloaded.score(str(companies[0]["id"])) is not None

    def test_saving_without_a_model_is_refused(self, tmp_path: Path, seeded_repository) -> None:
        service = GnnRiskService(repository=seeded_repository)
        with pytest.raises(ValidationError):
            service.save(tmp_path / "model.json")

    def test_loading_a_missing_file_is_refused(self, tmp_path: Path, seeded_repository) -> None:
        service = GnnRiskService(repository=seeded_repository)
        with pytest.raises(ValidationError):
            service.load(tmp_path / "absent.json")

    def test_the_description_reports_the_state(self, labelled_graph) -> None:
        repository, labels = labelled_graph
        service = GnnRiskService(repository=repository)
        assert service.describe()["ready"] is False
        service.train(labels)
        assert service.describe()["ready"] is True
        assert "lastReport" in service.describe()


class TestTwoScoreContract:
    def test_the_baseline_is_unchanged_by_the_model(self, labelled_graph, rule_engine) -> None:
        """Adding a model must not shift the baseline score.

        The two scores are reported side by side precisely so that the
        explainable one stays comparable across runs.
        """
        repository, labels = labelled_graph
        service = build_service(repository, rule_engine, config=RiskServiceConfig())
        companies = repository.find_nodes("Company")
        entity_id = str(companies[0]["id"])

        before = service.score(entity_id)
        assert before.model_score is None

        gnn = GnnRiskService(repository=repository)
        gnn.train(labels)
        service.gnn = gnn

        after = service.score(entity_id)
        assert after.score == before.score
        assert after.model_score is not None
        assert after.model_score_version is not None

    def test_the_gap_is_the_distance_between_the_two_scores(
        self, labelled_graph, rule_engine
    ) -> None:
        repository, labels = labelled_graph
        service = build_service(repository, rule_engine, config=RiskServiceConfig())
        gnn = GnnRiskService(repository=repository)
        gnn.train(labels)
        service.gnn = gnn

        companies = repository.find_nodes("Company")
        score = service.score(str(companies[0]["id"]))
        assert score.score_gap == pytest.approx(score.model_score - score.score, abs=0.01)

    def test_the_gap_is_none_without_a_model(self, seeded_repository, rule_engine) -> None:
        service = build_service(seeded_repository, rule_engine)
        companies = seeded_repository.find_nodes("Company")
        score = service.score(str(companies[0]["id"]))
        assert score.score_gap is None

    def test_the_baseline_score_is_still_explainable(self, labelled_graph, rule_engine) -> None:
        """The baseline must keep its indicator breakdown even with a model."""
        repository, labels = labelled_graph
        service = build_service(repository, rule_engine, config=RiskServiceConfig())
        gnn = GnnRiskService(repository=repository)
        gnn.train(labels)
        service.gnn = gnn

        companies = repository.find_nodes("Company")
        score = service.score(str(companies[0]["id"]))
        assert score.readings
        assert score.explain()
        assert "权重" in score.explain()

    def test_a_fresh_risk_score_has_no_model_fields(self) -> None:
        score = RiskScore(entity_id="c1", score=50)
        assert score.model_score is None
        assert score.score_gap is None

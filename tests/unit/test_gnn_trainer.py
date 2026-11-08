"""Unit tests for the GNN training loop.

The trainer is the one part of the platform whose behaviour cannot be
checked by reading the code: whether it converges, whether it reports
honest metrics, and whether it refuses to train on data that cannot support
a claim. These tests exercise it end to end on a small synthetic graph.
"""

from __future__ import annotations

import numpy as np
import pytest

from fin_kg.core.errors import ValidationError
from fin_kg.risk.gnn.features import FEATURE_NAMES, FeatureExtractor
from fin_kg.risk.gnn.model import GraphSAGEConfig, NumpySAGE
from fin_kg.risk.gnn.trainer import (
    GNNTrainer,
    LabelStore,
    WeakSupervisor,
)


@pytest.fixture
def labelled_graph(seeded_repository):
    """A graph with enough labelled nodes to train on.

    The seeded repository already contains a company cluster with guarantee
    chains; the labels mark one cluster as risky and the rest as safe.
    """
    companies = seeded_repository.find_nodes("Company")
    if len(companies) < 12:
        pytest.skip("the seeded graph is too small to train on")
    labels = LabelStore()
    for index, company in enumerate(companies):
        labels.set(company["id"], 1 if index % 4 == 0 else 0)
    return seeded_repository, labels


class TestLabelStore:
    def test_set_and_get(self) -> None:
        store = LabelStore()
        store.set("c1", 1)
        assert store.get("c1") == 1

    def test_an_unknown_entity_is_negative(self) -> None:
        assert LabelStore().get("nobody") == 0

    def test_the_labels_are_coerced_to_integers(self) -> None:
        store = LabelStore()
        store.set("c1", 1.9)  # type: ignore[arg-type]
        assert store.get("c1") == 1


class TestWeakSupervisor:
    def test_no_alerts_means_negative(self) -> None:
        assert WeakSupervisor(min_severity="high").label([]) == 0

    def test_a_critical_alert_means_positive(self) -> None:
        assert WeakSupervisor(min_severity="high").label(["critical"]) == 1

    def test_a_warning_alert_does_not_reach_the_high_threshold(self) -> None:
        assert WeakSupervisor(min_severity="high").label(["warning"]) == 0

    def test_the_highest_severity_wins(self) -> None:
        assert WeakSupervisor(min_severity="major").label(["info", "critical"]) == 1

    def test_the_threshold_is_configurable(self) -> None:
        assert WeakSupervisor(min_severity="info").label(["info"]) == 1


class TestGNNTrainer:
    def test_training_needs_enough_labels(self, seeded_repository) -> None:
        trainer = GNNTrainer(FeatureExtractor(seeded_repository))
        labels = LabelStore()
        labels.set("only-one", 1)
        with pytest.raises(ValidationError):
            trainer.train(labels)

    def test_training_produces_a_model_and_a_report(self, labelled_graph) -> None:
        repository, labels = labelled_graph
        trainer = GNNTrainer(
            FeatureExtractor(repository),
            config=GraphSAGEConfig(in_dim=len(FEATURE_NAMES), hidden_dim=8, epochs=20),
        )
        report = trainer.train(labels)
        assert trainer.model is not None
        assert trainer.last_report is report
        assert report.train_size > 0
        assert report.val_size > 0
        assert "finalAccuracy" in report.metrics

    def test_the_report_records_the_positive_count(self, labelled_graph) -> None:
        repository, labels = labelled_graph
        trainer = GNNTrainer(FeatureExtractor(repository))
        report = trainer.train(labels)
        assert report.positives == sum(labels.labels.values())

    def test_a_training_set_without_positives_is_flagged(self, labelled_graph) -> None:
        repository, _labels = labelled_graph
        labels = LabelStore()
        for node in repository.find_nodes("Company"):
            labels.set(node["id"], 0)
        trainer = GNNTrainer(FeatureExtractor(repository))
        report = trainer.train(labels)
        assert any("正样本" in warning for warning in report.warnings)

    def test_the_metrics_are_reported_when_the_split_allows(self, labelled_graph) -> None:
        repository, labels = labelled_graph
        trainer = GNNTrainer(FeatureExtractor(repository))
        report = trainer.train(labels)
        # Either the split produced both classes and the metrics exist, or a
        # warning explains why they do not. Both are honest outcomes.
        if report.metrics.get("f1") is not None:
            assert 0.0 <= report.metrics["f1"] <= 1.0
            assert 0.0 <= report.metrics["rocAuc"] <= 1.0
        else:
            assert report.warnings

    def test_predict_before_training_is_refused(self, labelled_graph) -> None:
        repository, _labels = labelled_graph
        trainer = GNNTrainer(FeatureExtractor(repository))
        with pytest.raises(ValidationError):
            trainer.predict(repository and None)  # type: ignore[arg-type]

    def test_predict_returns_a_probability_per_node(self, labelled_graph) -> None:
        repository, labels = labelled_graph
        extractor = FeatureExtractor(repository)
        trainer = GNNTrainer(extractor)
        trainer.train(labels)
        companies = repository.find_nodes("Company")
        subgraph = extractor.extract([companies[0]["id"]], max_hops=1)
        probabilities = trainer.predict(subgraph)
        assert set(probabilities) == set(subgraph.node_ids)
        for value in probabilities.values():
            assert 0.0 <= value <= 1.0

    def test_the_report_is_serialisable(self, labelled_graph) -> None:
        repository, labels = labelled_graph
        trainer = GNNTrainer(FeatureExtractor(repository))
        payload = trainer.train(labels).as_dict()
        assert set(payload) == {
            "metrics",
            "trainSize",
            "valSize",
            "positives",
            "warnings",
        }


class TestFeatureExtraction:
    def test_the_features_have_the_declared_width(self, seeded_repository) -> None:
        extractor = FeatureExtractor(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        assert companies
        subgraph = extractor.extract([companies[0]["id"]], max_hops=1)
        assert subgraph.features.shape[1] == len(FEATURE_NAMES)

    def test_the_neighbourhood_grows_with_the_hops(self, seeded_repository) -> None:
        extractor = FeatureExtractor(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        assert companies
        one_hop = extractor.extract([companies[0]["id"]], max_hops=1)
        two_hop = extractor.extract([companies[0]["id"]], max_hops=2)
        assert two_hop.num_nodes >= one_hop.num_nodes

    def test_max_nodes_is_respected(self, seeded_repository) -> None:
        extractor = FeatureExtractor(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        subgraph = extractor.extract(
            [company["id"] for company in companies], max_hops=2, max_nodes=5
        )
        assert subgraph.num_nodes <= 5

    def test_the_edge_index_matches_the_node_count(self, seeded_repository) -> None:
        extractor = FeatureExtractor(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        subgraph = extractor.extract([companies[0]["id"]], max_hops=2)
        if subgraph.edge_index.size:
            assert subgraph.edge_index.max() < subgraph.num_nodes

    def test_the_description_reports_the_shape(self, seeded_repository) -> None:
        extractor = FeatureExtractor(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        payload = extractor.extract([companies[0]["id"]], max_hops=1).describe()
        assert payload["featureDim"] == len(FEATURE_NAMES)
        assert payload["nodes"] >= 1


class TestModelPersistence:
    def test_a_saved_model_reloads_with_the_same_predictions(
        self, tmp_path, labelled_graph
    ) -> None:
        repository, labels = labelled_graph
        extractor = FeatureExtractor(repository)
        trainer = GNNTrainer(extractor)
        trainer.train(labels)

        path = tmp_path / "model.json"
        trainer.model.save(path)
        restored = NumpySAGE.load(path)

        companies = repository.find_nodes("Company")
        subgraph = extractor.extract([companies[0]["id"]], max_hops=1)
        features = subgraph.features
        before = trainer.model.predict_proba(features, subgraph.edge_index)
        after = restored.predict_proba(features, subgraph.edge_index)
        assert np.allclose(before, after)

    def test_the_config_survives_the_round_trip(self, tmp_path) -> None:
        model = NumpySAGE(GraphSAGEConfig(in_dim=5, hidden_dim=7, out_dim=1))
        path = tmp_path / "model.json"
        model.save(path)
        restored = NumpySAGE.load(path)
        assert restored.config.in_dim == 5
        assert restored.config.hidden_dim == 7

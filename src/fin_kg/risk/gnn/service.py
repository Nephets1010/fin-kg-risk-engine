"""GNN risk scoring service.

This module wires the GraphSAGE model into the risk pipeline. It exists
because the model, the feature extractor and the label store were each
implemented and unit-tested, but nothing connected them to the scoring
service — the model was never trained and its score was never produced.

The design keeps the two scores separate, as decided in ADR-0004:

* the **baseline** score (weighted indicator composite) is what the rule
  engine thresholds against, because it is fully explainable;
* the **model** score is an additional signal that a reviewer can compare
  against the baseline, never a replacement for it.

The model is optional. Without a trained model the service reports
``modelScore = None`` and nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fin_kg.core.errors import ValidationError
from fin_kg.core.logging import get_logger
from fin_kg.graph.repository import GraphRepository
from fin_kg.risk.gnn.features import FeatureExtractor
from fin_kg.risk.gnn.model import GraphSAGEConfig, NumpySAGE
from fin_kg.risk.gnn.trainer import GNNTrainer, LabelStore, TrainingReport

logger = get_logger(__name__)


@dataclass(slots=True)
class ModelScore:
    """The model's opinion about one entity."""

    entity_id: str
    probability: float
    model_version: str = "graphsage-1.0.0"
    subgraph_nodes: int = 0

    @property
    def score(self) -> float:
        """The probability expressed on the 0-100 risk scale."""
        return round(self.probability * 100.0, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entityId": self.entity_id,
            "probability": round(self.probability, 6),
            "score": self.score,
            "modelVersion": self.model_version,
            "subgraphNodes": self.subgraph_nodes,
        }


@dataclass(slots=True)
class GnnRiskService:
    """Trains and applies the GraphSAGE risk classifier.

    The service owns the model lifecycle: train it, persist it, load it
    back and score entities with it. Every step is explicit — there is no
    implicit training at startup, because a training run needs labels and
    must not happen as a side effect of serving a request.
    """

    repository: GraphRepository
    extractor: FeatureExtractor = field(init=False)
    trainer: GNNTrainer = field(init=False)
    model: NumpySAGE | None = None
    model_path: Path | None = None
    last_report: TrainingReport | None = None

    def __post_init__(self) -> None:
        self.extractor = FeatureExtractor(self.repository)
        self.trainer = GNNTrainer(self.extractor)

    # -- training -----------------------------------------------------------

    def train(
        self,
        labels: LabelStore,
        *,
        seed_ids: list[str] | None = None,
        max_hops: int = 2,
        max_nodes: int = 5_000,
        model_path: Path | None = None,
    ) -> TrainingReport:
        """Train the model and, when asked, persist it.

        The report is returned even when the metrics are weak — the caller
        decides whether a weak model is good enough. What the trainer will
        not do is hide the weakness: a validation split without positives
        produces a warning rather than a flattering metric.
        """
        report = self.trainer.train(
            labels, seed_ids=seed_ids, max_hops=max_hops, max_nodes=max_nodes
        )
        self.model = self.trainer.model
        self.last_report = report
        if model_path is not None:
            self.save(model_path)
        logger.info(
            "GNN trained: nodes=%s positives=%s metrics=%s",
            report.train_size + report.val_size,
            report.positives,
            report.metrics,
        )
        return report

    def save(self, path: Path) -> Path:
        """Persist the trained model."""
        if self.model is None:
            raise ValidationError("there is no trained model to save")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.model.save(path)
        self.model_path = path
        return path

    def load(self, path: Path) -> GnnRiskService:
        """Load a trained model from disk."""
        path = Path(path)
        if not path.exists():
            raise ValidationError("the model file does not exist", details={"path": str(path)})
        model = NumpySAGE.load(path)
        self.model = model
        # The trainer owns the inference helper, so a loaded model has to be
        # handed to it as well — otherwise ``predict`` would refuse to run.
        self.trainer.model = model
        self.model_path = path
        logger.info("GNN model loaded from %s", path)
        return self

    @property
    def is_ready(self) -> bool:
        """Whether a trained model is available."""
        return self.model is not None

    # -- inference ----------------------------------------------------------

    def score(self, entity_id: str, *, max_hops: int = 2) -> ModelScore | None:
        """Score one entity, or ``None`` when no model is trained."""
        if self.model is None:
            return None
        subgraph = self.extractor.extract([entity_id], max_hops=max_hops)
        if entity_id not in subgraph.node_ids:
            return None
        probabilities = self.trainer.predict(subgraph)
        probability = probabilities.get(entity_id)
        if probability is None:
            return None
        return ModelScore(
            entity_id=entity_id,
            probability=probability,
            subgraph_nodes=subgraph.num_nodes,
        )

    def score_many(self, entity_ids: list[str], *, max_hops: int = 2) -> list[ModelScore]:
        """Score several entities with a single subgraph extraction."""
        if self.model is None or not entity_ids:
            return []
        subgraph = self.extractor.extract(entity_ids, max_hops=max_hops)
        probabilities = self.trainer.predict(subgraph)
        results: list[ModelScore] = []
        for entity_id in entity_ids:
            probability = probabilities.get(entity_id)
            if probability is None:
                continue
            results.append(
                ModelScore(
                    entity_id=entity_id,
                    probability=probability,
                    subgraph_nodes=subgraph.num_nodes,
                )
            )
        return results

    def describe(self) -> dict[str, Any]:
        """Describe the model state, for the API and the CLI."""
        payload: dict[str, Any] = {
            "ready": self.is_ready,
            "modelPath": str(self.model_path) if self.model_path else None,
        }
        if self.last_report is not None:
            payload["lastReport"] = self.last_report.as_dict()
        return payload


def build_gnn_service(
    repository: GraphRepository, *, config: GraphSAGEConfig | None = None
) -> GnnRiskService:
    """Build the GNN service for a repository."""
    service = GnnRiskService(repository=repository)
    if config is not None:
        service.trainer.config = config
    return service


__all__ = ["GnnRiskService", "ModelScore", "build_gnn_service"]

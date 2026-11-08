"""GNN training and inference.

Training data is the scarce resource: real risk labels are rare and
confidential. The trainer therefore supports three label sources:

1. **Weak supervision** — labels derived from the baseline rule engine. Any
   entity that triggers a high-severity rule is treated as positive.
2. **Public datasets** — an externally supplied label file, e.g. CN-DBpedia
   derived risk annotations.
3. **Manual labels** — a CSV of ``entity_id,label`` reviewed by a risk
   analyst.

The model is evaluated with a stratified split and reported with precision,
recall, F1 and ROC-AUC. Because the positive class is rare, the F1 score and
the PR-AUC are the metrics that matter — accuracy alone would be
misleading, and the trainer logs a warning when the split is too small for
the metric to be trustworthy.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from fin_kg.core.errors import ValidationError
from fin_kg.core.logging import get_logger
from fin_kg.risk.gnn.features import FeatureExtractor, SubGraph, normalize_features
from fin_kg.risk.gnn.model import GraphSAGEConfig, NumpySAGE

logger = get_logger(__name__)


@dataclass(slots=True)
class TrainingReport:
    """Outcome of one training run."""

    metrics: dict[str, float] = field(default_factory=dict)
    train_size: int = 0
    val_size: int = 0
    positives: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "trainSize": self.train_size,
            "valSize": self.val_size,
            "positives": self.positives,
            "warnings": list(self.warnings),
        }


def precision_recall_f1(
    predictions: np.ndarray, labels: np.ndarray, threshold: float = 0.5
) -> tuple[float, float, float]:
    """Binary precision, recall and F1 at a threshold."""
    predicted = predictions >= threshold
    actual = labels >= 0.5
    true_positive = float(np.sum(predicted & actual))
    false_positive = float(np.sum(predicted & ~actual))
    false_negative = float(np.sum(~predicted & actual))
    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 0.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def roc_auc(predictions: np.ndarray, labels: np.ndarray) -> float:
    """Area under the ROC curve, computed with the rank method."""
    positives = float(np.sum(labels >= 0.5))
    negatives = float(len(labels) - positives)
    if positives == 0 or negatives == 0:
        return 0.0
    order = np.argsort(predictions)
    ranks = np.empty(len(predictions), dtype=np.float64)
    ranks[order] = np.arange(1, len(predictions) + 1)
    rank_sum = float(np.sum(ranks[labels >= 0.5]))
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


class WeakSupervisor:
    """Derives weak labels from the baseline rule engine.

    An entity is labelled positive when the rule engine fires a rule of at
    least ``min_severity`` for it.
    """

    def __init__(self, *, min_severity: str = "high") -> None:
        self.min_severity = min_severity
        self._order = {"info": 0, "warning": 1, "major": 2, "critical": 3}

    def label(self, alert_severities: Sequence[str]) -> int:
        """Return 1 when the highest severity reaches the threshold."""
        best = 0
        for severity in alert_severities:
            best = max(best, self._order.get(severity, 0))
        return 1 if best >= self._order.get(self.min_severity, 2) else 0


class LabelStore:
    """Loads and stores entity labels."""

    def __init__(self) -> None:
        self.labels: dict[str, int] = {}

    def set(self, entity_id: str, label: int) -> None:
        self.labels[entity_id] = int(label)

    def get(self, entity_id: str) -> int:
        return self.labels.get(entity_id, 0)

    @classmethod
    def from_csv(cls, path: Path) -> "LabelStore":
        """Load labels from a CSV with ``entity_id,label`` columns."""
        store = cls()
        path = Path(path)
        if not path.exists():
            raise ValidationError("label file not found", details={"path": str(path)})
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "entity_id" not in reader.fieldnames:
                raise ValidationError(
                    "label file must contain an 'entity_id' column",
                    details={"path": str(path), "columns": reader.fieldnames},
                )
            for row in reader:
                entity_id = str(row.get("entity_id", "")).strip()
                if not entity_id:
                    continue
                try:
                    store.set(entity_id, int(float(row.get("label", 0))))
                except (TypeError, ValueError):
                    continue
        logger.info("loaded %s labels from %s", len(store.labels), path.name)
        return store

    def save(self, path: Path) -> None:
        """Persist the labels as CSV."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["entity_id", "label"])
            for entity_id, label in sorted(self.labels.items()):
                writer.writerow([entity_id, label])


class GNNTrainer:
    """Trains the GraphSAGE risk classifier."""

    def __init__(
        self,
        extractor: FeatureExtractor,
        *,
        config: GraphSAGEConfig | None = None,
        validation_ratio: float = 0.2,
        seed: int = 42,
    ) -> None:
        self.extractor = extractor
        self.config = config
        self.validation_ratio = validation_ratio
        self.seed = seed
        self.model: NumpySAGE | None = None
        self.last_report: TrainingReport | None = None

    def train(
        self,
        labels: LabelStore,
        *,
        seed_ids: list[str] | None = None,
        max_hops: int = 2,
        max_nodes: int = 5_000,
    ) -> TrainingReport:
        """Train the model on the labelled entities and their neighbourhoods."""
        labelled_ids = [entity_id for entity_id in labels.labels if entity_id in labels.labels]
        if len(labelled_ids) < 10:
            raise ValidationError(
                "at least 10 labelled entities are required to train",
                details={"labelled": len(labelled_ids)},
            )

        subgraph = self.extractor.extract(
            seed_ids or labelled_ids, max_hops=max_hops, max_nodes=max_nodes
        )
        features = normalize_features(subgraph.features)
        targets = np.array(
            [labels.get(node_id) for node_id in subgraph.node_ids], dtype=np.float64
        )

        rng = np.random.default_rng(self.seed)
        indices = rng.permutation(len(subgraph.node_ids))
        split = max(1, int(len(indices) * (1 - self.validation_ratio)))
        train_idx, val_idx = indices[:split], indices[split:]
        if len(val_idx) == 0:
            val_idx = train_idx

        train_mask = np.zeros(len(subgraph.node_ids), dtype=bool)
        train_mask[train_idx] = True
        val_mask = ~train_mask

        config = self.config or GraphSAGEConfig(in_dim=features.shape[1], seed=self.seed)
        self.model = NumpySAGE(config)

        train_features = features[train_mask]
        train_labels = targets[train_mask]
        train_edges = self._restrict_edges(subgraph.edge_index, train_mask)

        report = TrainingReport(
            train_size=int(train_mask.sum()),
            val_size=int(val_mask.sum()),
            positives=int(targets.sum()),
        )
        if int(train_labels.sum()) == 0:
            report.warnings.append("训练集中没有正样本，模型无法学习风险模式")

        train_result = self.model.train(train_features, train_edges, train_labels)
        report.metrics.update(train_result)

        val_features = features[val_mask]
        val_labels = targets[val_mask]
        if len(val_labels) and (val_labels.sum() > 0) and (val_labels.sum() < len(val_labels)):
            predictions = self.model.predict_proba(val_features, self._restrict_edges(subgraph.edge_index, val_mask))
            precision, recall, f1 = precision_recall_f1(predictions, val_labels)
            report.metrics.update(
                {
                    "precision": round(precision, 4),
                    "recall": round(recall, 4),
                    "f1": round(f1, 4),
                    "rocAuc": round(roc_auc(predictions, val_labels), 4),
                }
            )
            if len(val_labels) < 20:
                report.warnings.append("验证集过小，F1 指标不稳定，建议扩大标注样本")
        else:
            report.warnings.append("验证集缺少正样本或负样本，无法计算完整分类指标")

        self.last_report = report
        logger.info("GNN training finished: %s", report.as_dict())
        return report

    @staticmethod
    def _restrict_edges(edge_index: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """Keep only the edges whose both endpoints are inside the mask."""
        if edge_index.size == 0:
            return edge_index
        keep = mask[edge_index[0]] & mask[edge_index[1]]
        return edge_index[:, keep]

    def predict(self, subgraph: SubGraph) -> dict[str, float]:
        """Predict the risk probability of every node in a subgraph."""
        if self.model is None:
            raise ValidationError("the model has not been trained")
        features = normalize_features(subgraph.features)
        probabilities = self.model.predict_proba(features, subgraph.edge_index)
        return {
            node_id: float(probability)
            for node_id, probability in zip(subgraph.node_ids, probabilities)
        }


__all__ = [
    "GNNTrainer",
    "LabelStore",
    "TrainingReport",
    "WeakSupervisor",
    "precision_recall_f1",
    "roc_auc",
]

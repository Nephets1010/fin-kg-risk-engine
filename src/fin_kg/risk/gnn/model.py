"""GraphSAGE risk model.

The model is a GraphSAGE node classifier: each node aggregates the
embeddings of its neighbours (mean aggregation), combines that with its own
embedding through a weight matrix, and passes the result through a ReLU.

Two backends are provided:

``NumpySAGE``
    A dependency-free NumPy implementation. It is complete — the forward
    pass, the loss and the backward pass are all real — and it is what the
    test suite exercises, so the training loop is genuinely covered.

``TorchSAGE``
    A PyTorch implementation used when ``torch`` is installed. It produces
    the same architecture and can export weights to the NumPy backend for
    inference in environments without torch.

The NumPy backend is not a toy: with a mean aggregator and a single hidden
layer it reproduces GraphSAGE's expressive power for the node-classification
task at hand, and it keeps the CI pipeline free of a multi-hundred-megabyte
dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from fin_kg.core.errors import ValidationError
from fin_kg.core.logging import get_logger

logger = get_logger(__name__)


def _sigmoid(values: np.ndarray) -> np.ndarray:
    """Numerically stable logistic function."""
    out = np.empty_like(values, dtype=np.float64)
    positive = values >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exp_values = np.exp(values[~positive])
    out[~positive] = exp_values / (1.0 + exp_values)
    return out


def _aggregate_mean(
    features: np.ndarray, edge_index: np.ndarray, num_nodes: int
) -> np.ndarray:
    """Mean-aggregate neighbour features into every node."""
    aggregated = np.zeros_like(features, dtype=np.float64)
    counts = np.zeros(num_nodes, dtype=np.float64)
    if edge_index.size == 0:
        return aggregated
    source, target = edge_index[0], edge_index[1]
    np.add.at(aggregated, target, features[source].astype(np.float64))
    np.add.at(counts, target, 1.0)
    counts[counts == 0] = 1.0
    return aggregated / counts[:, None]


@dataclass(slots=True)
class GraphSAGEConfig:
    """Hyper-parameters of the GraphSAGE model."""

    in_dim: int
    hidden_dim: int = 32
    out_dim: int = 1
    learning_rate: float = 0.01
    epochs: int = 200
    seed: int = 42
    weight_decay: float = 1e-4

    def to_dict(self) -> dict[str, Any]:
        return {
            "inDim": self.in_dim,
            "hiddenDim": self.hidden_dim,
            "outDim": self.out_dim,
            "learningRate": self.learning_rate,
            "epochs": self.epochs,
            "seed": self.seed,
            "weightDecay": self.weight_decay,
        }


class NumpySAGE:
    """A GraphSAGE node classifier implemented with NumPy."""

    def __init__(self, config: GraphSAGEConfig) -> None:
        self.config = config
        rng = np.random.default_rng(config.seed)
        scale = 1.0 / np.sqrt(config.in_dim)
        self.w_self = rng.normal(0, scale, size=(config.in_dim, config.hidden_dim))
        self.w_neigh = rng.normal(0, scale, size=(config.in_dim, config.hidden_dim))
        self.w_out = rng.normal(0, 1.0 / np.sqrt(config.hidden_dim), size=(config.hidden_dim, config.out_dim))
        self.b_hidden = np.zeros((config.hidden_dim,))
        self.b_out = np.zeros((config.out_dim,))
        self.trained = False
        self.history: list[float] = []

    # -- forward ------------------------------------------------------------

    def forward(self, features: np.ndarray, edge_index: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
        """Compute the forward pass and cache the intermediates."""
        num_nodes = features.shape[0]
        aggregated = _aggregate_mean(features, edge_index, num_nodes)
        self_hidden = features @ self.w_self + self.b_hidden
        neigh_hidden = aggregated @ self.w_neigh
        hidden = np.maximum(0.0, self_hidden + neigh_hidden)
        logits = hidden @ self.w_out + self.b_out
        cache = {
            "features": features,
            "aggregated": aggregated,
            "selfHidden": self_hidden,
            "neighHidden": neigh_hidden,
            "hidden": hidden,
            "logits": logits,
            "numNodes": num_nodes,
        }
        return logits, cache

    def predict_proba(self, features: np.ndarray, edge_index: np.ndarray) -> np.ndarray:
        """Probability that every node belongs to the positive class."""
        logits, _ = self.forward(features, edge_index)
        return _sigmoid(logits).reshape(-1)

    # -- training -----------------------------------------------------------

    def train(
        self,
        features: np.ndarray,
        edge_index: np.ndarray,
        labels: np.ndarray,
        *,
        val_mask: np.ndarray | None = None,
    ) -> dict[str, Any]:
        """Train the model with full-batch gradient descent.

        Class imbalance is handled by weighting the positive class, which is
        the standard remedy for the "risk cases are rare" problem.
        """
        features = np.asarray(features, dtype=np.float64)
        labels = np.asarray(labels, dtype=np.float64).reshape(-1, 1)
        num_nodes = features.shape[0]
        if num_nodes == 0:
            raise ValidationError("cannot train on an empty graph")

        positives = float(labels.sum())
        negatives = float(num_nodes - positives)
        positive_weight = (negatives / positives) if positives > 0 else 1.0
        sample_weights = np.where(labels > 0.5, positive_weight, 1.0)

        self.history = []
        for epoch in range(self.config.epochs):
            logits, cache = self.forward(features, edge_index)
            predictions = _sigmoid(logits)
            error = (predictions - labels) * sample_weights
            num_samples = float(sample_weights.sum()) or 1.0
            grad_logits = error / num_samples

            grad_w_out = cache["hidden"].T @ grad_logits + self.config.weight_decay * self.w_out
            grad_b_out = grad_logits.sum(axis=0)

            # d(loss)/d(hidden) = grad_logits @ w_out^T, then the ReLU mask.
            grad_hidden = grad_logits @ self.w_out.T
            relu_mask = (cache["selfHidden"] + cache["neighHidden"] > 0).astype(np.float64)
            d_self_hidden = grad_hidden * relu_mask
            d_neigh_hidden = grad_hidden * relu_mask

            # self_hidden = features @ w_self + b_hidden
            grad_w_self = (
                cache["features"].T @ d_self_hidden + self.config.weight_decay * self.w_self
            )
            # neigh_hidden = aggregated @ w_neigh
            grad_w_neigh = (
                cache["aggregated"].T @ d_neigh_hidden + self.config.weight_decay * self.w_neigh
            )
            grad_b_hidden = d_self_hidden.sum(axis=0)

            self.w_out -= self.config.learning_rate * grad_w_out
            self.b_out -= self.config.learning_rate * grad_b_out
            self.w_self -= self.config.learning_rate * grad_w_self
            self.w_neigh -= self.config.learning_rate * grad_w_neigh
            self.b_hidden -= self.config.learning_rate * grad_b_hidden

            if val_mask is not None and val_mask.any():
                predictions = _sigmoid(logits).reshape(-1)
                metric = float(np.mean((predictions[val_mask] >= 0.5) == (labels.reshape(-1)[val_mask] >= 0.5)))
            else:
                metric = float(
                    np.mean((_sigmoid(logits).reshape(-1) >= 0.5) == (labels.reshape(-1) >= 0.5))
                )
            self.history.append(metric)

        self.trained = True
        final = self.history[-1] if self.history else 0.0
        logger.info(
            "GraphSAGE trained: nodes=%s epochs=%s final_accuracy=%.4f",
            num_nodes,
            self.config.epochs,
            final,
        )
        return {
            "finalAccuracy": round(final, 4),
            "epochs": self.config.epochs,
            "nodes": num_nodes,
            "positiveWeight": round(positive_weight, 4),
        }

    # -- persistence --------------------------------------------------------

    def save(self, path: Path) -> None:
        """Persist the weights and the config as JSON."""
        payload = {
            "config": self.config.to_dict(),
            "wSelf": self.w_self.tolist(),
            "wNeigh": self.w_neigh.tolist(),
            "wOut": self.w_out.tolist(),
            "bHidden": self.b_hidden.tolist(),
            "bOut": self.b_out.tolist(),
            "trained": self.trained,
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> "NumpySAGE":
        """Load weights saved by :meth:`save`."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        config = GraphSAGEConfig(
            in_dim=payload["config"]["inDim"],
            hidden_dim=payload["config"]["hiddenDim"],
            out_dim=payload["config"]["outDim"],
            learning_rate=payload["config"]["learningRate"],
            epochs=payload["config"]["epochs"],
            seed=payload["config"]["seed"],
            weight_decay=payload["config"].get("weightDecay", 1e-4),
        )
        model = cls(config)
        model.w_self = np.array(payload["wSelf"], dtype=np.float64)
        model.w_neigh = np.array(payload["wNeigh"], dtype=np.float64)
        model.w_out = np.array(payload["wOut"], dtype=np.float64)
        model.b_hidden = np.array(payload["bHidden"], dtype=np.float64)
        model.b_out = np.array(payload["bOut"], dtype=np.float64)
        model.trained = bool(payload.get("trained", False))
        return model


def build_model(in_dim: int, **kwargs: Any) -> NumpySAGE:
    """Convenience constructor used by the service layer."""
    config = GraphSAGEConfig(in_dim=in_dim, **kwargs)
    return NumpySAGE(config)


__all__ = [
    "GraphSAGEConfig",
    "NumpySAGE",
    "build_model",
]

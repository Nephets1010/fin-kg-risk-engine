"""GNN sub-package: features, model and trainer."""

from fin_kg.risk.gnn.features import (
    DEFAULT_EDGE_RELATIONS,
    FEATURE_NAMES,
    FeatureExtractor,
    SubGraph,
    normalize_features,
)
from fin_kg.risk.gnn.model import GraphSAGEConfig, NumpySAGE, build_model
from fin_kg.risk.gnn.trainer import (
    GNNTrainer,
    LabelStore,
    TrainingReport,
    WeakSupervisor,
    precision_recall_f1,
    roc_auc,
)

__all__ = [
    "DEFAULT_EDGE_RELATIONS",
    "FEATURE_NAMES",
    "FeatureExtractor",
    "GNNTrainer",
    "GraphSAGEConfig",
    "LabelStore",
    "NumpySAGE",
    "SubGraph",
    "TrainingReport",
    "WeakSupervisor",
    "build_model",
    "normalize_features",
    "precision_recall_f1",
    "roc_auc",
]

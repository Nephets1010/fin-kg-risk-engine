"""Graph feature extraction.

The GNN needs a feature matrix and an edge index. This module builds both
from the graph repository.

Two feature strategies are supported:

``structural``
    Node features derived purely from the graph topology: in-degree,
    out-degree, total degree, PageRank-ish centrality and the aggregated
    transmissibility of the incident edges. Works without any training data,
    which matters because risk labels are extremely scarce.

``attribute``
    Node features that mix the structural features with the entity's own
    attributes (industry one-hot, risk score). Requires the attributes to be
    present on the node.

The structural strategy is the default because it is the only one that works
on a cold graph with no labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from fin_kg.core.logging import get_logger
from fin_kg.domain.relations import RelationType
from fin_kg.graph.repository import GraphRepository

logger = get_logger(__name__)

#: The structural feature vector, in a fixed order.
FEATURE_NAMES: tuple[str, ...] = (
    "in_degree",
    "out_degree",
    "total_degree",
    "log_degree",
    "mean_transmissibility",
    "max_transmissibility",
    "guarantee_out",
    "hold_out",
    "risk_score",
)

#: Relation types used as undirected edges when building the subgraph.
DEFAULT_EDGE_RELATIONS: tuple[RelationType, ...] = (
    RelationType.HOLD,
    RelationType.GUARANTEE,
    RelationType.CONTROLS,
    RelationType.LEGAL_REP,
    RelationType.DIRECTOR_OF,
    RelationType.EMPLOYEE_OF,
    RelationType.INVESTS_IN,
    RelationType.MERGER_OF,
    RelationType.CUSTOMER_OF,
    RelationType.SUPPLIER_OF,
    RelationType.RELATED,
)


@dataclass(slots=True)
class SubGraph:
    """A sampled subgraph ready for GNN inference."""

    node_ids: list[str]
    features: np.ndarray
    edge_index: np.ndarray
    edge_weight: np.ndarray
    labels: np.ndarray | None = None

    @property
    def num_nodes(self) -> int:
        return len(self.node_ids)

    @property
    def num_edges(self) -> int:
        return int(self.edge_index.shape[1]) if self.edge_index.size else 0

    def index_of(self, node_id: str) -> int:
        """Position of a node inside the subgraph."""
        return self.node_ids.index(node_id)

    def describe(self) -> dict[str, Any]:
        """Summary statistics used by the diagnostics endpoint."""
        return {
            "nodes": self.num_nodes,
            "edges": self.num_edges,
            "featureDim": int(self.features.shape[1]),
            "featureNames": list(FEATURE_NAMES),
        }


class FeatureExtractor:
    """Builds :class:`SubGraph` objects from a graph repository."""

    def __init__(
        self,
        repository: GraphRepository,
        *,
        edge_relations: tuple[RelationType, ...] = DEFAULT_EDGE_RELATIONS,
        risk_score_source: str = "stored",
    ) -> None:
        self.repository = repository
        self.edge_relations = edge_relations
        self.risk_score_source = risk_score_source

    def extract(
        self,
        seed_ids: list[str],
        *,
        max_hops: int = 2,
        max_nodes: int = 2_000,
    ) -> SubGraph:
        """Extract the k-hop neighbourhood of the seeds as a feature matrix."""
        node_ids = self._collect(seed_ids, max_hops=max_hops, max_nodes=max_nodes)
        index_of = {node_id: index for index, node_id in enumerate(node_ids)}

        snapshot = getattr(self.repository, "snapshot", None)
        features = np.zeros((len(node_ids), len(FEATURE_NAMES)), dtype=np.float32)
        edges: list[tuple[int, int]] = []
        weights: list[float] = []

        if snapshot is not None:
            for node_id in node_ids:
                incident = snapshot.adjacency.get(node_id, [])
                in_degree = 0
                out_degree = 0
                guarantee_out = 0
                hold_out = 0
                transmissibilities: list[float] = []
                for edge in incident:
                    if edge["source"] == node_id:
                        out_degree += 1
                        if edge.get("relationType") == "GUARANTEE":
                            guarantee_out += 1
                        if edge.get("relationType") == "HOLD":
                            hold_out += 1
                    else:
                        in_degree += 1
                    transmissibilities.append(float(edge.get("transmissibility") or 0.0))
                    other = edge["target"] if edge["source"] == node_id else edge["source"]
                    if other in index_of:
                        edges.append((index_of[node_id], index_of[other]))
                        weights.append(float(edge.get("transmissibility") or 0.0))

                node = snapshot.nodes.get(node_id, {})
                total = in_degree + out_degree
                features[index_of[node_id]] = (
                    in_degree,
                    out_degree,
                    total,
                    float(np.log1p(total)),
                    float(np.mean(transmissibilities)) if transmissibilities else 0.0,
                    float(np.max(transmissibilities)) if transmissibilities else 0.0,
                    guarantee_out,
                    hold_out,
                    float(node.get("riskScore") or 0.0),
                )
        else:
            for node_id in node_ids:
                features[index_of[node_id]] = 0.0

        edge_index = (
            np.array(edges, dtype=np.int64).T if edges else np.zeros((2, 0), dtype=np.int64)
        )
        edge_weight = np.array(weights, dtype=np.float32)

        logger.info(
            "extracted subgraph nodes=%s edges=%s from %s seeds",
            len(node_ids),
            len(edges),
            len(seed_ids),
        )
        return SubGraph(
            node_ids=node_ids,
            features=features,
            edge_index=edge_index,
            edge_weight=edge_weight,
        )

    def _collect(self, seed_ids: list[str], *, max_hops: int, max_nodes: int) -> list[str]:
        """Breadth-first collection of the neighbourhood node ids."""
        collected: list[str] = []
        seen: set[str] = set()
        frontier = [node_id for node_id in seed_ids if node_id]
        for node_id in frontier:
            if node_id not in seen:
                seen.add(node_id)
                collected.append(node_id)

        for _ in range(max_hops):
            next_frontier: list[str] = []
            for node_id in frontier:
                for path in self.repository.traverse(
                    node_id, self.edge_relations, max_hops=1, direction="both"
                ):
                    for candidate in path.node_ids()[1:]:
                        if candidate not in seen:
                            seen.add(candidate)
                            collected.append(candidate)
                            next_frontier.append(candidate)
                            if len(collected) >= max_nodes:
                                return collected
            if not next_frontier:
                break
            frontier = next_frontier
        return collected


def normalize_features(features: np.ndarray) -> np.ndarray:
    """Standardise each feature column to zero mean and unit variance.

    Constant columns become all-zero instead of producing NaNs.
    """
    if features.size == 0:
        return features
    means = features.mean(axis=0)
    stds = features.std(axis=0)
    stds[stds < 1e-8] = 1.0
    return (features - means) / stds


__all__ = [
    "DEFAULT_EDGE_RELATIONS",
    "FEATURE_NAMES",
    "FeatureExtractor",
    "SubGraph",
    "normalize_features",
]

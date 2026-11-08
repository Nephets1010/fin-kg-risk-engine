"""Similar-case retrieval.

When an analyst reviews a new company, the most useful thing the system can
offer is a comparable: "companies with a similar structure had these
outcomes". This module finds such comparables.

Two similarity dimensions are used:

* **Structural similarity** — the shape of the graph around the entity:
  degree profile, guarantee exposure, ownership concentration, event mix.
  This is computed with a cosine similarity over a normalised feature
  vector.
* **Risk-profile similarity** — how the entity scores across the risk
  indicators.

Both are computed locally, without an embedding model, so the results are
reproducible and explainable: the module returns *which* dimensions matched,
not just a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from fin_kg.core.logging import get_logger
from fin_kg.domain.risk import RiskScore
from fin_kg.graph.repository import GraphRepository

logger = get_logger(__name__)

#: The structural dimensions used for comparison.
SIMILARITY_DIMENSIONS: tuple[str, ...] = (
    "guaranteeExposure",
    "holdingConcentration",
    "eventDensity",
    "degreeProfile",
    "riskScore",
)


@dataclass(slots=True)
class SimilarCase:
    """One comparable case."""

    entity_id: str
    entity_name: str
    similarity: float
    dimensions: dict[str, float] = field(default_factory=dict)
    risk_score: float = 0.0
    risk_level: str = "low"
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "entityId": self.entity_id,
            "entityName": self.entity_name,
            "similarity": round(self.similarity, 4),
            "dimensions": {k: round(v, 4) for k, v in self.dimensions.items()},
            "riskScore": round(self.risk_score, 2),
            "riskLevel": self.risk_level,
            "reason": self.reason,
        }


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    """Cosine similarity of two vectors, with the zero-vector convention."""
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return float(np.dot(left, right) / (left_norm * right_norm))


class SimilarCaseFinder:
    """Finds companies structurally similar to a reference entity."""

    def __init__(self, repository: GraphRepository) -> None:
        self.repository = repository

    def feature_vector(self, entity_id: str, score: RiskScore | None = None) -> np.ndarray:
        """Build the comparison vector of one entity."""
        edges = self.repository.neighbours(entity_id)
        guarantee_exposure = sum(
            float(edge.get("guaranteed_amount") or 0.0)
            for edge in edges
            if edge.get("relationType") == "GUARANTEE" and edge.get("source") == entity_id
        )
        holdings = [
            float(edge.get("percent") or 0.0)
            for edge in edges
            if edge.get("relationType") == "HOLD" and edge.get("target") == entity_id
        ]
        concentration = max(holdings) if holdings else 0.0
        events = sum(
            1
            for edge in edges
            if edge.get("relationType") == "INVOLVED_IN" and edge.get("target") == entity_id
        )
        degree = len(edges)

        risk_value = (
            score.score
            if score is not None
            else float((self.repository.get_node(entity_id) or {}).get("riskScore") or 0.0)
        )
        return np.array(
            [
                np.log1p(guarantee_exposure),
                concentration,
                float(events),
                np.log1p(degree),
                risk_value / 100.0,
            ],
            dtype=np.float64,
        )

    def find(
        self,
        entity_id: str,
        *,
        top_k: int = 10,
        min_similarity: float = 0.3,
        exclude_related: bool = False,
    ) -> list[SimilarCase]:
        """Return the most similar companies to ``entity_id``."""
        node = self.repository.get_node(entity_id)
        if node is None:
            return []

        reference = self.feature_vector(entity_id)
        related_ids = {entity_id}
        if exclude_related:
            # Only the directly connected parties are dropped: a comparable
            # case that is itself part of the subject's guarantee chain is
            # not an independent reference point.
            for edge in self.repository.neighbours(entity_id):
                related_ids.add(str(edge.get("source")))
                related_ids.add(str(edge.get("target")))

        results: list[SimilarCase] = []
        for candidate in self.repository.find_nodes("Company"):
            candidate_id = str(candidate.get("id", ""))
            if not candidate_id or candidate_id in related_ids:
                continue
            candidate_vector = self.feature_vector(candidate_id)
            similarity = cosine_similarity(reference, candidate_vector)
            if similarity < min_similarity:
                continue
            dimensions = {
                name: float(
                    cosine_similarity(
                        reference[index : index + 1], candidate_vector[index : index + 1]
                    )
                )
                for index, name in enumerate(SIMILARITY_DIMENSIONS)
            }
            candidate_node = candidate
            results.append(
                SimilarCase(
                    entity_id=candidate_id,
                    entity_name=str(candidate_node.get("name", candidate_id)),
                    similarity=similarity,
                    dimensions=dimensions,
                    risk_score=float(candidate_node.get("riskScore") or 0.0),
                    risk_level=str(candidate_node.get("riskLevel") or "low"),
                    reason=self._explain(dimensions),
                )
            )

        results.sort(key=lambda item: item.similarity, reverse=True)
        return results[:top_k]

    @staticmethod
    def _explain(dimensions: dict[str, float]) -> str:
        """Name the two dimensions that matched best."""
        ordered = sorted(dimensions.items(), key=lambda item: item[1], reverse=True)
        if not ordered:
            return "无明显相似维度"
        top = ordered[:2]
        return "、".join(f"{name} 相似度 {value:.2f}" for name, value in top)


__all__ = ["SIMILARITY_DIMENSIONS", "SimilarCase", "SimilarCaseFinder", "cosine_similarity"]

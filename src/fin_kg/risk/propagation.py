"""Risk propagation.

When an entity takes on risk, that risk travels along the graph. The
propagation engine simulates that travel and reports which other entities are
exposed, and through which path.

Model
-----
Each edge carries a *transmissibility* in ``[0, 1]``. Risk flowing across a
path decays multiplicatively::

    propagated(source_score) = source_score × Π(transmissibility_i) × decay^(hops - 1)

``decay`` is the per-hop penalty (0.6 by default): the further the risk
travels, the less certain the link. The resulting score is clamped to 100.

Why not a full simulation?
--------------------------
A full diffusion simulation is expensive and produces scores that are hard to
explain to a reviewer. The multiplicative decay model is:

* **cheap** — one bounded BFS per source entity,
* **explainable** — the weakest link on the path is exactly what the analyst
  needs to look at,
* **incremental** — only entities whose neighbourhood changed need to be
  recomputed, which is what brings the end-to-end latency from 24 hours down
  to under an hour.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date

from fin_kg.core.logging import get_logger
from fin_kg.domain.relations import RelationType
from fin_kg.domain.risk import RiskScore, TransmissionPath
from fin_kg.graph.repository import GraphRepository
from fin_kg.observability.metrics import PROPAGATED

logger = get_logger(__name__)

#: Relation types risk flows along, in decreasing order of transmissibility.
DEFAULT_TRANSMISSION_RELATIONS: tuple[RelationType, ...] = (
    RelationType.GUARANTEE,
    RelationType.CONTROLS,
    RelationType.HOLD,
    RelationType.SHAREHOLDER_OF,
    RelationType.MERGER_OF,
    RelationType.INVESTS_IN,
    RelationType.LEGAL_REP,
    RelationType.DIRECTOR_OF,
    RelationType.EMPLOYEE_OF,
    RelationType.CUSTOMER_OF,
    RelationType.SUPPLIER_OF,
    RelationType.RELATED,
)


@dataclass(slots=True)
class PropagationResult:
    """Everything the propagation engine found for one source entity."""

    source_id: str
    paths: list[TransmissionPath]
    affected: list[dict[str, object]]
    max_hops_reached: int = 0


class RiskPropagator:
    """Propagates risk along the graph and records the paths."""

    def __init__(
        self,
        repository: GraphRepository,
        *,
        max_hops: int = 3,
        decay: float = 0.6,
        relations: tuple[RelationType, ...] = DEFAULT_TRANSMISSION_RELATIONS,
        min_transmissibility: float = 0.0,
    ) -> None:
        self.repository = repository
        self.max_hops = max_hops
        self.decay = decay
        self.relations = relations
        self.min_transmissibility = min_transmissibility

    def propagate(
        self,
        source_score: RiskScore,
        *,
        today: date | None = None,
    ) -> PropagationResult:
        """Propagate the risk of one entity and return the exposed entities."""
        if source_score.score <= 0:
            return PropagationResult(source_id=source_score.entity_id, paths=[], affected=[])

        paths = self._explore(source_score.entity_id)
        propagated: list[TransmissionPath] = []
        best_by_node: dict[str, TransmissionPath] = {}

        for path in paths:
            score = self._propagated_score(source_score.score, path)
            if score <= 0:
                continue
            path.propagated_score = round(score, 2)
            propagated.append(path)
            terminal = path.node_ids[-1]
            existing = best_by_node.get(terminal)
            if existing is None or path.propagated_score > existing.propagated_score:
                best_by_node[terminal] = path

        # One entry per exposed entity, not per path. An entity reachable
        # through two chains would otherwise be reported twice, which reads
        # as two separate exposures to the analyst. The strongest path wins.
        affected: list[dict[str, object]] = [
            {
                "id": path.node_ids[-1],
                "name": path.node_names[-1] if path.node_names else "",
                "hops": path.hops,
                "score": path.propagated_score,
                "path": path.to_text(),
            }
            for path in best_by_node.values()
        ]

        affected.sort(key=lambda item: float(item["score"] or 0), reverse=True)
        max_hops = max((path.hops for path in propagated), default=0)
        logger.info(
            "propagated %s score %.1f to %s entities (max %s hops)",
            source_score.entity_id,
            source_score.score,
            len(affected),
            max_hops,
        )
        PROPAGATED.inc()
        return PropagationResult(
            source_id=source_score.entity_id,
            paths=propagated,
            affected=affected,
            max_hops_reached=max_hops,
        )

    def _explore(self, source_id: str) -> list[TransmissionPath]:
        """Enumerate bounded paths, keeping only the strongest per endpoint."""
        results: dict[str, TransmissionPath] = {}

        def walk(
            node_ids: list[str], node_names: list[str], edges: list[dict], seen: set[str]
        ) -> None:
            if len(edges) >= self.max_hops:
                return
            current = node_ids[-1]
            for path in self.repository.traverse(
                current, self.relations, max_hops=1, direction="out"
            ):
                relation = path.relations[0]
                transmissibility = float(relation.get("transmissibility") or 0.0)
                if transmissibility < self.min_transmissibility:
                    continue
                next_id = path.node_ids()[-1]
                if next_id in seen:
                    continue
                new_edges = [*edges, relation]
                new_ids = [*node_ids, next_id]
                new_names = [*node_names, path.nodes[-1].get("name", "") if path.nodes else ""]
                candidate = TransmissionPath(
                    node_ids=new_ids,
                    node_names=new_names,
                    relation_types=[
                        str(relation.get("relationType", "")) for relation in new_edges
                    ],
                    transmissibilities=[
                        float(edge.get("transmissibility") or 0.0) for edge in new_edges
                    ],
                    propagated_score=0.0,
                    hops=len(new_edges),
                )
                key = "->".join(new_ids)
                existing = results.get(key)
                if existing is None:
                    results[key] = candidate
                walk(new_ids, new_names, new_edges, seen | {next_id})

        source_node = self.repository.get_node(source_id) or {}
        source_name = str(source_node.get("name") or source_id)
        walk([source_id], [source_name], [], {source_id})
        return list(results.values())

    def _propagated_score(self, source_score: float, path: TransmissionPath) -> float:
        """Apply the multiplicative decay model to one path."""
        score = source_score
        for index, transmissibility in enumerate(path.transmissibilities):
            score *= transmissibility
            if index > 0:
                score *= self.decay
        return max(0.0, min(100.0, score))

    def propagate_batch(
        self,
        scores: list[RiskScore],
        *,
        today: date | None = None,
    ) -> dict[str, PropagationResult]:
        """Propagate the risk of several source entities."""
        return {score.entity_id: self.propagate(score, today=today) for score in scores}


def bfs_affected_entities(
    repository: GraphRepository,
    source_id: str,
    *,
    max_hops: int = 3,
    relations: tuple[RelationType, ...] = DEFAULT_TRANSMISSION_RELATIONS,
) -> dict[str, int]:
    """Return the hop distance from ``source_id`` to every reachable entity."""
    distances: dict[str, int] = {source_id: 0}
    queue: deque[tuple[str, int]] = deque([(source_id, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for path in repository.traverse(current, relations, max_hops=1, direction="out"):
            next_id = path.node_ids()[-1]
            if next_id not in distances:
                distances[next_id] = depth + 1
                queue.append((next_id, depth + 1))
    return distances


__all__ = [
    "DEFAULT_TRANSMISSION_RELATIONS",
    "PropagationResult",
    "RiskPropagator",
    "bfs_affected_entities",
]

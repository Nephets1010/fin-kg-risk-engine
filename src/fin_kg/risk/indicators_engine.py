"""Risk indicator computation.

The indicator engine reads the graph and produces one
:class:`~fin_kg.domain.risk.IndicatorReading` per indicator per entity.

Two execution strategies are supported:

:class:`GraphIndicatorEngine`
    Executes the Cypher declared by each indicator against the live graph.

:class:`LocalIndicatorEngine`
    Computes the same indicators from an in-memory snapshot. It exists so
    the scoring model can be unit tested and so the offline demo works
    without a running Neo4j. Both engines implement
    :class:`IndicatorEngine`, and the risk service depends on the interface
    rather than on either implementation.
"""

from __future__ import annotations

import abc
from datetime import date, timedelta
from typing import Any

from fin_kg.core.logging import get_logger
from fin_kg.domain.indicators import RISK_INDICATORS, IndicatorSpec
from fin_kg.domain.risk import IndicatorReading
from fin_kg.graph.repository import GraphRepository

logger = get_logger(__name__)

#: Default look-back window for the indicators.
DEFAULT_WINDOW_DAYS = 365


class IndicatorEngine(abc.ABC):
    """Computes indicator readings for entities."""

    @abc.abstractmethod
    def measure(self, spec: IndicatorSpec, entity_id: str, *, since: date) -> float | None:
        """Return the raw measurement of one indicator for one entity."""

    def readings(self, entity_id: str, *, since: date) -> list[IndicatorReading]:
        """Compute every indicator reading for one entity."""
        results: list[IndicatorReading] = []
        for spec in RISK_INDICATORS:
            try:
                measurement = self.measure(spec, entity_id, since=since)
            except Exception as exc:  # a broken indicator must not kill the run
                logger.warning(
                    "indicator %s failed for %s: %s", spec.id, entity_id, exc, exc_info=True
                )
                measurement = None
            results.append(
                IndicatorReading(
                    indicator_id=spec.id,
                    indicator_name=spec.name,
                    version=spec.version,
                    category=spec.category.value,
                    measurement=measurement,
                    sub_score=spec.score(measurement),
                    weight=spec.weight,
                    unit=spec.unit,
                    detail=(
                        f"{spec.name} = {measurement:g} {spec.unit}"
                        if measurement is not None
                        else f"{spec.name} 无可用数据"
                    ),
                )
            )
        return results


class GraphIndicatorEngine(IndicatorEngine):
    """Executes the indicator Cypher against the live graph."""

    def __init__(self, repository: GraphRepository) -> None:
        self.repository = repository

    def measure(self, spec: IndicatorSpec, entity_id: str, *, since: date) -> float | None:
        rows = self.repository.execute_read(
            spec.measurement_cypher,
            {"id": entity_id, "since": since.isoformat()},
        )
        if not rows:
            return None
        row = rows[0]
        for key in ("value", "count", "total"):
            if key in row and row[key] is not None:
                return float(row[key])
        numeric = [value for value in row.values() if isinstance(value, (int, float))]
        return float(numeric[0]) if numeric else None


class LocalIndicatorEngine(IndicatorEngine):
    """Computes indicators from an in-memory graph snapshot.

    The measurements are deliberately computed with the same semantics as the
    Cypher versions: the window filter, the aggregation and the null handling
    match one-to-one.
    """

    def __init__(self, repository: GraphRepository) -> None:
        self.repository = repository

    def measure(self, spec: IndicatorSpec, entity_id: str, *, since: date) -> float | None:
        snapshot = getattr(self.repository, "snapshot", None)
        if snapshot is None:
            return None

        if (
            spec.id.startswith("IND-LIT")
            or spec.id.startswith("IND-PEN")
            or spec.id.startswith("IND-RTE")
            or spec.id.startswith("IND-NEG")
            or spec.id.startswith("IND-DEF")
        ):
            return self._count_events(snapshot, spec, entity_id, since)
        if spec.id == "IND-ENF-001":
            exposure = self._sum_event_amounts(snapshot, entity_id, since, "enforcement")
            return self._ratio_to_capital(snapshot, entity_id, exposure)
        if spec.id == "IND-GUA-001":
            exposure = self._sum_guarantees(snapshot, entity_id)
            return self._ratio_to_capital(snapshot, entity_id, exposure)
        if spec.id == "IND-CHN-001":
            return self._max_chain_depth(snapshot, entity_id)
        if spec.id == "IND-HLD-001":
            return self._max_shareholding(snapshot, entity_id)
        if spec.id == "IND-CTL-001":
            return self._legal_rep_risk(snapshot, entity_id)

        # An indicator that this engine cannot measure locally is not an
        # error — the local store is a development and offline path, and the
        # Cypher engine remains the reference implementation. But silently
        # returning ``None`` would make a newly added indicator look like
        # "no data" instead of "not implemented here", so it is logged.
        logger.warning(
            "indicator %s is not implemented for the local store; "
            "it contributes no score here (use the Neo4j store for it)",
            spec.id,
        )
        return None

    @staticmethod
    def _ratio_to_capital(snapshot: Any, entity_id: str, exposure: float) -> float:
        """Divide an exposure by the registered capital of the entity.

        The registered capital is the only scale reference that is present on
        every company in the graph, which is what makes the ratio comparable
        across entities of very different sizes.
        """
        node = snapshot.nodes.get(entity_id, {})
        capital = float(node.get("regCapital") or 0.0)
        if capital <= 0:
            return 0.0
        return exposure / capital

    @staticmethod
    def _event_types_for(spec: IndicatorSpec) -> set[str]:
        mapping = {
            "IND-LIT-001": {"litigation", "enforcement"},
            "IND-PEN-001": {"penalty"},
            "IND-RTE-001": {"rating_downgrade"},
            "IND-NEG-001": {"negative_news"},
            "IND-DEF-001": {"default"},
        }
        return mapping.get(spec.id, set())

    def _count_events(
        self, snapshot: Any, spec: IndicatorSpec, entity_id: str, since: date
    ) -> float | None:
        wanted = self._event_types_for(spec)
        count = 0
        for edge in snapshot.adjacency.get(entity_id, []):
            if edge.get("relationType") != "INVOLVED_IN":
                continue
            target = snapshot.nodes.get(edge["target"], {})
            if target.get("eventType") not in wanted:
                continue
            occurred = target.get("occurredDate")
            if occurred and str(occurred) < since.isoformat():
                continue
            count += 1
        return float(count)

    def _sum_event_amounts(
        self, snapshot: Any, entity_id: str, since: date, event_type: str
    ) -> float | None:
        total = 0.0
        for edge in snapshot.adjacency.get(entity_id, []):
            if edge.get("relationType") != "INVOLVED_IN":
                continue
            target = snapshot.nodes.get(edge["target"], {})
            if target.get("eventType") != event_type:
                continue
            occurred = target.get("occurredDate")
            if occurred and str(occurred) < since.isoformat():
                continue
            total += float(target.get("amount") or 0.0)
        return total

    def _sum_guarantees(self, snapshot: Any, entity_id: str) -> float | None:
        total = 0.0
        for edge in snapshot.adjacency.get(entity_id, []):
            if edge.get("relationType") != "GUARANTEE":
                continue
            if edge["source"] != entity_id:
                continue
            total += float(edge.get("guaranteed_amount") or 0.0)
        return total

    def _max_chain_depth(self, snapshot: Any, entity_id: str) -> float | None:
        depth = 0
        frontier = {entity_id}
        seen = {entity_id}
        for _ in range(5):
            next_frontier: set[str] = set()
            for current in frontier:
                for edge in snapshot.adjacency.get(current, []):
                    if edge.get("relationType") != "GUARANTEE":
                        continue
                    if edge["source"] != current:
                        continue
                    if edge["target"] in seen:
                        continue
                    next_frontier.add(edge["target"])
                    seen.add(edge["target"])
            if not next_frontier:
                break
            depth += 1
            frontier = next_frontier
        return float(depth)

    def _max_shareholding(self, snapshot: Any, entity_id: str) -> float | None:
        """The largest shareholder's stake.

        ``None`` is returned when no shareholder is known. Returning ``0``
        would be wrong: for the ``LOWER_IS_RISKIER`` direction a stake of
        zero means "extremely dispersed" and would produce the maximum risk
        score for a company whose shareholder data is simply missing.
        """
        best = 0.0
        found = False
        for edge in snapshot.adjacency.get(entity_id, []):
            if edge.get("relationType") != "HOLD":
                continue
            if edge["target"] != entity_id:
                continue
            found = True
            best = max(best, float(edge.get("percent") or 0.0))
        return best if found else None

    def _legal_rep_risk(self, snapshot: Any, entity_id: str) -> float | None:
        best = 0.0
        for edge in snapshot.adjacency.get(entity_id, []):
            if edge.get("relationType") != "LEGAL_REP":
                continue
            if edge["target"] != entity_id:
                continue
            person = snapshot.nodes.get(edge["source"], {})
            best = max(best, float(person.get("riskScore") or 0.0))
        return best


def window_start(days: int = DEFAULT_WINDOW_DAYS, *, today: date | None = None) -> date:
    """The start of the indicator look-back window."""
    return (today or date.today()) - timedelta(days=days)


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "GraphIndicatorEngine",
    "IndicatorEngine",
    "LocalIndicatorEngine",
    "window_start",
]

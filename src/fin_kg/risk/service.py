"""The risk service.

This is the façade the API and the scheduler talk to. It orchestrates the
four stages of the risk pipeline:

``score``       compute the composite risk score of an entity
``propagate``   spread that risk along the graph
``evaluate``    run the rule engine and produce alerts
``persist``     write the score and the alerts back to the graph

The service is deliberately a thin orchestrator: all the business logic
lives in the modules it calls, which keeps each of them independently
testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fin_kg.core.errors import NotFoundError
from fin_kg.core.logging import get_logger
from fin_kg.domain.risk import RiskAlert, RiskAssessment, RiskScore
from fin_kg.graph.repository import GraphRepository
from fin_kg.risk.engine import RuleEngine, alert_fingerprint
from fin_kg.risk.indicators_engine import IndicatorEngine, LocalIndicatorEngine
from fin_kg.risk.propagation import RiskPropagator
from fin_kg.risk.scoring import RiskScorer

logger = get_logger(__name__)


@dataclass(slots=True)
class RiskServiceConfig:
    """Tuning parameters of the service."""

    alert_threshold: float = 60.0
    critical_threshold: float = 85.0
    max_hops: int = 3
    decay: float = 0.6
    window_days: int = 365
    persist_scores: bool = True


@dataclass(slots=True)
class RiskService:
    """Coordinates scoring, propagation and rule evaluation."""

    repository: GraphRepository
    rules: RuleEngine
    indicator_engine: IndicatorEngine
    config: RiskServiceConfig = field(default_factory=RiskServiceConfig)
    _scorer: RiskScorer | None = None
    _propagator: RiskPropagator | None = None

    def __post_init__(self) -> None:
        self._scorer = RiskScorer(
            self.indicator_engine,
            alert_threshold=self.config.alert_threshold,
            critical_threshold=self.config.critical_threshold,
            window_days=self.config.window_days,
        )
        self._propagator = RiskPropagator(
            self.repository,
            max_hops=self.config.max_hops,
            decay=self.config.decay,
        )

    # -- scoring ------------------------------------------------------------

    def score(self, entity_id: str, *, today: date | None = None) -> RiskScore:
        """Score one entity, raising when it does not exist."""
        node = self.repository.get_node(entity_id)
        if node is None:
            raise NotFoundError("entity not found", details={"entity_id": entity_id})
        return self._score_known(entity_id, node, today=today)

    def _score_known(
        self, entity_id: str, node: dict[str, Any], *, today: date | None = None
    ) -> RiskScore:
        assert self._scorer is not None
        return self._scorer.score(
            entity_id,
            entity_name=str(node.get("name", "")),
            entity_type=self._infer_type(node),
            today=today,
        )

    @staticmethod
    def _infer_type(node: dict[str, Any]) -> str:
        """Infer the entity type from the stored properties."""
        if "riskLevel" in node and "industry" in node:
            return "company"
        if "role" in node:
            return "person"
        if "eventType" in node:
            return "event"
        if "productType" in node:
            return "product"
        return "company"

    def score_many(
        self, entity_ids: Iterable[str], *, today: date | None = None
    ) -> list[RiskScore]:
        """Score a batch of entities, skipping the ones that do not exist."""
        assert self._scorer is not None
        scores: list[RiskScore] = []
        for entity_id in entity_ids:
            node = self.repository.get_node(entity_id)
            if node is None:
                continue
            scores.append(self._score_known(entity_id, node, today=today))
        return scores

    # -- propagation --------------------------------------------------------

    def propagate(self, score: RiskScore, *, today: date | None = None) -> list[Any]:
        """Propagate one entity's risk and return the affected entities."""
        assert self._propagator is not None
        return self._propagator.propagate(score, today=today).affected

    def exposure_circle(self, entity_id: str, *, max_hops: int = 2) -> list[dict[str, Any]]:
        """The entities directly and indirectly exposed to ``entity_id``."""
        assert self._propagator is not None
        score = self.score(entity_id)
        result = self._propagator.propagate(score)
        return [item for item in result.affected if int(item.get("hops", 0)) <= max_hops]

    # -- assessment ---------------------------------------------------------

    def assess(self, entity_id: str, *, today: date | None = None) -> RiskAssessment:
        """Run the full assessment of one entity."""
        score = self.score(entity_id, today=today)
        assert self._propagator is not None
        propagation = self._propagator.propagate(score, today=today)
        assessment = self.rules.evaluate_assessment(score, propagation.paths)
        if self.config.persist_scores:
            self._persist_score(score)
        return assessment

    def assess_many(
        self, entity_ids: Iterable[str], *, today: date | None = None
    ) -> list[RiskAssessment]:
        """Assess a batch of entities."""
        return [self.assess(entity_id, today=today) for entity_id in entity_ids]

    def rank(self, *, limit: int = 20, today: date | None = None) -> list[RiskScore]:
        """The highest-risk entities in the graph."""
        companies = self.repository.find_nodes("Company")
        scored = [self._score_known(node.get("id", ""), node, today=today) for node in companies]
        scored.sort(key=lambda item: item.score, reverse=True)
        result = scored[:limit]
        if self.config.persist_scores:
            for score in result:
                self._persist_score(score)
        return result

    def alerts(
        self,
        *,
        min_severity: str = "info",
        limit: int = 100,
        today: date | None = None,
    ) -> list[RiskAlert]:
        """Evaluate the rules over the whole graph and return the alerts."""
        order = {"info": 0, "warning": 1, "major": 2, "critical": 3}
        threshold = order.get(min_severity, 0)
        collected: list[RiskAlert] = []
        for score in self.rank(limit=limit * 3, today=today):
            assert self._propagator is not None
            propagation = self._propagator.propagate(score, today=today)
            for alert in self.rules.evaluate(
                score, propagation.paths, fingerprint_of=alert_fingerprint
            ):
                if order.get(alert.severity.value, 0) >= threshold:
                    collected.append(alert)
        collected.sort(key=lambda alert: (alert.severity.rank, alert.score), reverse=True)
        return collected[:limit]

    # -- persistence --------------------------------------------------------

    def _persist_score(self, score: RiskScore) -> None:
        """Write the computed score back onto the node."""
        properties = {
            "riskScore": score.score,
            "riskLevel": score.level.value,
            "riskModelVersion": score.model_version,
            "riskComputedAt": score.computed_at.isoformat(),
        }
        try:
            self.repository.update_properties(score.entity_id, properties)
        except Exception as exc:  # persistence must not break the assessment
            logger.warning("failed to persist score for %s: %s", score.entity_id, exc)


def build_service(
    repository: GraphRepository,
    rules: RuleEngine,
    *,
    config: RiskServiceConfig | None = None,
) -> RiskService:
    """Construct a :class:`RiskService` with the local indicator engine.

    The local engine is used by default because it works against both the
    in-memory and the Neo4j repository; the graph engine is selected when
    the caller explicitly wants server-side Cypher evaluation.
    """
    return RiskService(
        repository=repository,
        rules=rules,
        indicator_engine=LocalIndicatorEngine(repository),
        config=config or RiskServiceConfig(),
    )


__all__ = ["RiskService", "RiskServiceConfig", "build_service"]

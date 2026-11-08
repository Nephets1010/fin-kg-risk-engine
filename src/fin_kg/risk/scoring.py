"""Risk scoring model.

The baseline score is a weighted composite of the indicator sub-scores:

``score = Σ (sub_score_i × weight_i) / Σ weight_i``

Normalising by the sum of the weights (rather than assuming it is 1.0) means
a catalogue with missing or newly added indicators still produces a score on
the same 0-100 scale.

The GNN score is kept *separate* from the baseline score. Mixing a learned
model into the headline number would make the number unexplainable, which is
precisely what a risk analyst needs to avoid. The two scores are reported
side by side, and the fusion happens in the rule engine where the rule author
decides which one a given alert is based on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from fin_kg.domain.entities import RiskLevel
from fin_kg.domain.indicators import RISK_INDICATORS
from fin_kg.domain.risk import IndicatorReading, RiskScore
from fin_kg.risk.indicators_engine import IndicatorEngine, window_start


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """The intermediate numbers behind a composite score."""

    weighted_sum: float
    weight_total: float
    coverage: float
    missing_indicators: tuple[str, ...]


class RiskScorer:
    """Computes the composite risk score of an entity."""

    model_version = "baseline-1.0.0"

    def __init__(
        self,
        engine: IndicatorEngine,
        *,
        alert_threshold: float = 60.0,
        critical_threshold: float = 85.0,
        window_days: int = 365,
    ) -> None:
        self.engine = engine
        self.alert_threshold = alert_threshold
        self.critical_threshold = critical_threshold
        self.window_days = window_days

    def breakdown(self, readings: list[IndicatorReading]) -> ScoreBreakdown:
        """Compute the weighted sum, total weight and data coverage."""
        weighted = sum(reading.contribution for reading in readings)
        weight_total = sum(reading.weight for reading in readings)
        missing = tuple(
            reading.indicator_id for reading in readings if reading.measurement is None
        )
        coverage = (
            sum(1 for reading in readings if reading.measurement is not None)
            / len(readings)
            if readings
            else 0.0
        )
        return ScoreBreakdown(
            weighted_sum=weighted,
            weight_total=weight_total,
            coverage=coverage,
            missing_indicators=missing,
        )

    def score(
        self,
        entity_id: str,
        *,
        entity_name: str = "",
        entity_type: str = "company",
        today: date | None = None,
    ) -> RiskScore:
        """Score one entity."""
        since = window_start(self.window_days, today=today)
        readings = self.engine.readings(entity_id, since=since)
        breakdown = self.breakdown(readings)
        if breakdown.weight_total <= 0:
            score = 0.0
        else:
            score = breakdown.weighted_sum / breakdown.weight_total
        score = round(max(0.0, min(100.0, score)), 2)
        return RiskScore(
            entity_id=entity_id,
            entity_name=entity_name,
            entity_type=entity_type,
            score=score,
            level=RiskLevel.from_score(
                score, alert=self.alert_threshold, critical=self.critical_threshold
            ),
            readings=readings,
            model_version=self.model_version,
        )

    def score_many(
        self,
        entity_ids: list[str],
        *,
        names: dict[str, str] | None = None,
        today: date | None = None,
    ) -> list[RiskScore]:
        """Score a batch of entities."""
        lookup = names or {}
        return [
            self.score(
                entity_id,
                entity_name=lookup.get(entity_id, ""),
                today=today,
            )
            for entity_id in entity_ids
        ]

    def coverage_report(self, readings: list[IndicatorReading]) -> dict[str, object]:
        """Describe how well the indicators were populated."""
        breakdown = self.breakdown(readings)
        return {
            "coverage": round(breakdown.coverage, 4),
            "weightTotal": round(breakdown.weight_total, 4),
            "missing": list(breakdown.missing_indicators),
            "catalogueSize": len(RISK_INDICATORS),
        }


__all__ = ["RiskScorer", "ScoreBreakdown"]

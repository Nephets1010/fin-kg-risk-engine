"""Risk domain objects.

These types are the contract between the risk engine and everything
downstream (API, scheduler, research module). They are deliberately free of
framework imports so they can be unit tested without FastAPI or a database.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fin_kg.domain.entities import RiskLevel
from fin_kg.domain.provenance import utcnow


class IndicatorReading(BaseModel):
    """One indicator measurement for one entity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    indicator_id: str
    indicator_name: str
    version: str
    category: str
    measurement: float | None = None
    sub_score: float = Field(ge=0.0, le=100.0)
    weight: float = Field(ge=0.0, le=1.0)
    unit: str = ""
    detail: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def contribution(self) -> float:
        """Weighted contribution to the composite score."""
        return self.sub_score * self.weight


class RiskScore(BaseModel):
    """The composite risk assessment of one entity."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    entity_name: str = ""
    entity_type: str = "company"
    score: float = Field(ge=0.0, le=100.0)
    level: RiskLevel = RiskLevel.LOW
    readings: list[IndicatorReading] = Field(default_factory=list)
    model_version: str = "baseline-1.0.0"
    computed_at: datetime = Field(default_factory=utcnow)

    @property
    def top_contributors(self) -> list[IndicatorReading]:
        """The three readings that contributed the most, worst first."""
        return sorted(self.readings, key=lambda r: r.contribution, reverse=True)[:3]

    def explain(self) -> str:
        """A one-paragraph, human-readable justification."""
        if not self.readings:
            return f"{self.entity_name} 风险分 {self.score:.1f}（{self.level.value}），无可用指标读数。"
        parts = [
            f"{r.indicator_name} {r.sub_score:.0f}分×权重{r.weight:.2f}（贡献{r.contribution:.1f}）"
            for r in self.top_contributors
        ]
        return (
            f"{self.entity_name} 风险分 {self.score:.1f}（{self.level.value}）。"
            f"主要来源：{'；'.join(parts)}。"
        )


class AlertSeverity(str, Enum):
    """Severity of an emitted alert, ordered from least to most serious."""

    INFO = "info"
    WARNING = "warning"
    MAJOR = "major"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "major": 2, "critical": 3}[self.value]


class AlertStatus(str, Enum):
    """Lifecycle of an alert."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


class TransmissionPath(BaseModel):
    """A single path along which risk propagated."""

    model_config = ConfigDict(extra="forbid")

    node_ids: list[str]
    node_names: list[str] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list)
    transmissibilities: list[float] = Field(default_factory=list)
    propagated_score: float = Field(ge=0.0, le=100.0)
    hops: int = 0

    @property
    def bottleneck(self) -> float:
        """The weakest link in the path — the natural explanation anchor."""
        return min(self.transmissibilities) if self.transmissibilities else 0.0

    def to_text(self) -> str:
        """Render the path as ``A -[HOLD]-> B -[GUARANTEE]-> C``."""
        if not self.node_names:
            return " -> ".join(self.node_ids)
        parts = [self.node_names[0]]
        for index, relation in enumerate(self.relation_types):
            next_name = self.node_names[index + 1] if index + 1 < len(self.node_names) else "?"
            parts.append(f"-[{relation}]-> {next_name}")
        return " ".join(parts)


class RiskAlert(BaseModel):
    """An alert produced by the rule engine."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str
    rule_id: str
    rule_name: str
    severity: AlertSeverity = AlertSeverity.WARNING
    status: AlertStatus = AlertStatus.OPEN
    entity_id: str
    entity_name: str = ""
    entity_type: str = "company"
    score: float = Field(default=0.0, ge=0.0, le=100.0)
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    transmission_paths: list[TransmissionPath] = Field(default_factory=list)
    affected_entity_ids: list[str] = Field(default_factory=list)
    triggered_at: datetime = Field(default_factory=utcnow)
    model_version: str = "baseline-1.0.0"
    fingerprint: str = ""


class RiskAssessment(BaseModel):
    """The full result of assessing one entity."""

    model_config = ConfigDict(extra="forbid")

    score: RiskScore
    alerts: list[RiskAlert] = Field(default_factory=list)
    transmission_paths: list[TransmissionPath] = Field(default_factory=list)
    affected_entities: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "AlertSeverity",
    "AlertStatus",
    "IndicatorReading",
    "RiskAlert",
    "RiskAssessment",
    "RiskLevel",
    "RiskScore",
    "TransmissionPath",
]

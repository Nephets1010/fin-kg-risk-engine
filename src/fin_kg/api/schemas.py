"""API request and response schemas.

These are the wire contract. They are separate from the domain models on
purpose: the domain models describe the business, these describe the API, and
the two evolve for different reasons.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fin_kg.domain.entities import RiskLevel


class ErrorResponse(BaseModel):
    """The error envelope returned by every failing endpoint."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(examples=["not_found"])
    message: str
    details: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    """Liveness and readiness of the service and its dependencies."""

    status: str = Field(examples=["ok"])
    version: str
    environment: str
    uptimeSeconds: float
    dependencies: dict[str, str] = Field(default_factory=dict)


class GraphStatsResponse(BaseModel):
    """Node and relationship counts of the graph."""

    nodes: dict[str, int] = Field(default_factory=dict)
    relationships: dict[str, int] = Field(default_factory=dict)
    totalNodes: int = 0
    totalRelationships: int = 0


class EntityResponse(BaseModel):
    """A single graph entity."""

    id: str
    name: str
    labels: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class EntityListResponse(BaseModel):
    """A page of entities."""

    items: list[EntityResponse] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0


class PathResponse(BaseModel):
    """One path through the graph."""

    nodeIds: list[str] = Field(default_factory=list)
    nodeNames: list[str] = Field(default_factory=list)
    relationTypes: list[str] = Field(default_factory=list)
    hops: int = 0
    propagatedScore: float | None = None
    text: str = ""


class IndicatorReadingResponse(BaseModel):
    """One indicator measurement."""

    indicatorId: str
    indicatorName: str
    version: str
    category: str
    measurement: float | None = None
    subScore: float
    weight: float
    unit: str = ""
    detail: str | None = None


class RiskScoreResponse(BaseModel):
    """The risk assessment of one entity."""

    entityId: str
    entityName: str = ""
    entityType: str = "company"
    score: float
    level: RiskLevel
    modelVersion: str
    computedAt: datetime
    readings: list[IndicatorReadingResponse] = Field(default_factory=list)
    explanation: str = ""
    #: The GraphSAGE score, present only when a model has been trained.
    #: ``null`` means "no model available", not "the model says zero".
    modelScore: float | None = None
    #: The distance between the two scores — a large gap marks the entities
    #: where the graph structure says something the indicators do not.
    scoreGap: float | None = None


class RiskRankResponse(BaseModel):
    """A ranked list of risk scores."""

    items: list[RiskScoreResponse] = Field(default_factory=list)
    total: int = 0


class AlertResponse(BaseModel):
    """A risk alert."""

    alertId: str
    ruleId: str
    ruleName: str
    severity: str
    status: str
    entityId: str
    entityName: str = ""
    score: float
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    transmissionPaths: list[PathResponse] = Field(default_factory=list)
    triggeredAt: datetime


class AlertListResponse(BaseModel):
    """A list of alerts."""

    items: list[AlertResponse] = Field(default_factory=list)
    total: int = 0
    severities: dict[str, int] = Field(default_factory=dict)


class ExposureResponse(BaseModel):
    """The entities exposed to one entity's risk."""

    entityId: str
    entityName: str = ""
    score: float = 0.0
    affected: list[dict[str, Any]] = Field(default_factory=list)


class AskRequest(BaseModel):
    """A natural-language question.

    ``topK`` caps how many results the answer returns. It is an upper bound:
    when the question itself names a count ("前 10 家公司"), the smaller of
    the two wins. Leaving it unset keeps the planner's own default.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=500)
    topK: int | None = Field(default=None, ge=1, le=50)


class EvidenceResponse(BaseModel):
    """One piece of evidence backing an answer."""

    kind: str
    description: str
    nodeIds: list[str] = Field(default_factory=list)
    nodeNames: list[str] = Field(default_factory=list)
    relationTypes: list[str] = Field(default_factory=list)


class AnswerResponse(BaseModel):
    """The answer to a question, with its evidence path."""

    question: str
    answer: str
    intent: str
    confidence: float
    evidence: list[EvidenceResponse] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)


class SimilarCaseResponse(BaseModel):
    """One comparable case."""

    entityId: str
    entityName: str
    similarity: float
    dimensions: dict[str, float] = Field(default_factory=dict)
    riskScore: float = 0.0
    riskLevel: str = "low"
    reason: str = ""


class ReportSectionResponse(BaseModel):
    """One section of a research briefing."""

    title: str
    content: str = ""
    bullets: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class ReportResponse(BaseModel):
    """A complete research briefing."""

    entityId: str
    entityName: str
    generatedAt: str
    markdown: str = ""
    sections: list[ReportSectionResponse] = Field(default_factory=list)


class JobRequest(BaseModel):
    """A request to start a long-running job."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(default="synthetic", description="Source adapter name")
    since: str | None = Field(default=None, description="ISO date lower bound")
    limit: int | None = Field(default=None, ge=1, le=1_000_000)


class JobStatusResponse(BaseModel):
    """The status of a long-running job."""

    jobId: str
    jobType: str
    status: str
    createdAt: datetime
    updatedAt: datetime
    progress: int = 0
    total: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


class IngestReportResponse(BaseModel):
    """The outcome of an ingestion run."""

    sourceIds: list[str] = Field(default_factory=list)
    fetched: int = 0
    accepted: int = 0
    rejected: int = 0
    deduplicated: int = 0
    nodesWritten: int = 0
    relationsWritten: int = 0
    durationSeconds: float = 0.0
    cleaning: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AlertListResponse",
    "AlertResponse",
    "AnswerResponse",
    "EntityListResponse",
    "EntityResponse",
    "ErrorResponse",
    "EvidenceResponse",
    "ExposureResponse",
    "GraphStatsResponse",
    "HealthResponse",
    "IndicatorReadingResponse",
    "IngestReportResponse",
    "JobRequest",
    "JobStatusResponse",
    "PathResponse",
    "ReportResponse",
    "ReportSectionResponse",
    "RiskRankResponse",
    "RiskScoreResponse",
    "SimilarCaseResponse",
]

"""Node type definitions.

Four node labels carry the graph:

``Company``  legal entity — the risk anchor of the whole model
``Person``   natural person — director, legal representative, actual controller
``Event``    something that happened — enforcement, litigation, penalty, rating change
``Product``  financial product — bond, fund, structured product

Every node inherits :class:`GraphNode`, which supplies the identity, the
creation timestamp and the provenance list. Nodes are immutable value objects;
the graph store owns persistence.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fin_kg.domain.provenance import Provenance, utcnow


class Industry(str, Enum):
    """Normalised industry classification (CSRC-style buckets)."""

    FINANCE = "finance"
    REAL_ESTATE = "real_estate"
    MANUFACTURING = "manufacturing"
    ENERGY = "energy"
    TECHNOLOGY = "technology"
    CONSTRUCTION = "construction"
    TRADE = "trade"
    AGRICULTURE = "agriculture"
    TRANSPORT = "transport"
    HEALTHCARE = "healthcare"
    EDUCATION = "education"
    CULTURE_ENT = "culture_ent"
    OTHER = "other"


class CompanyStatus(str, Enum):
    """Registration status of a legal entity."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    REVOKED = "revoked"
    BANKRUPT = "bankrupt"
    UNKNOWN = "unknown"


class RiskLevel(str, Enum):
    """Ordinal risk banding used across the whole platform."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Numeric ordering, higher is worse."""
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}[self.value]

    @classmethod
    def from_score(cls, score: float, *, alert: float = 60.0, critical: float = 85.0) -> RiskLevel:
        """Map a 0-100 risk score onto a band."""
        if score >= critical:
            return cls.CRITICAL
        if score >= alert:
            return cls.HIGH
        if score >= alert / 2:
            return cls.MEDIUM
        return cls.LOW


class GraphNode(BaseModel):
    """Base class of every graph node."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    provenance: list[Provenance] = Field(default_factory=list)

    #: Neo4j label used when this node is persisted.
    label: str = "GraphNode"

    def label_set(self) -> set[str]:
        """Labels applied to this node in the graph store."""
        return {self.label, "Entity"}


class Company(GraphNode):
    """A legal entity.

    ``reg_capital`` is expressed in **万元** (ten thousand yuan), the unit
    used by the Chinese registry. Every monetary attribute of the platform
    uses the same unit, so that the ratios computed by the indicators are
    unit-consistent.
    """

    label: str = "Company"

    reg_capital: float | None = Field(default=None, ge=0)
    industry: Industry = Industry.OTHER
    status: CompanyStatus = CompanyStatus.UNKNOWN
    founded_date: date | None = None
    credit_code: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=128)
    risk_score: float = Field(default=0.0, ge=0.0, le=100.0)
    risk_level: RiskLevel = RiskLevel.LOW
    external_refs: dict[str, str] = Field(default_factory=dict)

    @field_validator("credit_code")
    @classmethod
    def _validate_credit_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        if len(cleaned) != 18:
            raise ValueError("credit_code must be an 18-character unified social credit code")
        return cleaned


class PersonRole(str, Enum):
    """Role a person holds with respect to a company."""

    LEGAL_REP = "legal_rep"  # 法定代表人
    DIRECTOR = "director"  # 董事
    SUPERVISOR = "supervisor"  # 监事
    ACTUAL_CONTROLLER = "actual_controller"  # 实际控制人
    SHAREHOLDER = "shareholder"  # 股东
    EXECUTIVE = "executive"  # 高管


class Person(GraphNode):
    """A natural person."""

    label: str = "Person"

    role: PersonRole = PersonRole.SHAREHOLDER
    risk_score: float = Field(default=0.0, ge=0.0, le=100.0)
    risk_level: RiskLevel = RiskLevel.LOW
    id_number_hash: str | None = Field(default=None, max_length=128)
    external_refs: dict[str, str] = Field(default_factory=dict)


class EventType(str, Enum):
    """Kinds of events recorded on the graph."""

    ENFORCEMENT = "enforcement"  # 被强制执行
    LITIGATION = "litigation"  # 涉诉
    PENALTY = "penalty"  # 行政处罚
    ARREARS = "arrears"  # 欠税/欠款
    DEFAULT = "default"  # 违约
    RATING_DOWNGRADE = "rating_downgrade"  # 评级下调
    EQUITY_FREEZE = "equity_freeze"  # 股权冻结
    BUSINESS_CHANGE = "business_change"  # 工商变更
    ABNORMAL_OPERATION = "abnormal_operation"  # 经营异常
    NEGATIVE_NEWS = "negative_news"  # 负面舆情
    OTHER = "other"


class Severity(str, Enum):
    """Intrinsic severity of an event, independent of any entity's score."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def weight(self) -> float:
        """Contribution weight used by the baseline score model."""
        return {"low": 0.2, "medium": 0.45, "high": 0.75, "critical": 1.0}[self.value]


class Event(GraphNode):
    """Something that happened, with a severity and a date."""

    label: str = "Event"

    event_type: EventType = EventType.OTHER
    severity: Severity = Severity.MEDIUM
    occurred_date: date | None = None
    amount: float | None = None
    court: str | None = Field(default=None, max_length=256)
    case_number: str | None = Field(default=None, max_length=128)
    description: str | None = None
    external_refs: dict[str, str] = Field(default_factory=dict)


class ProductType(str, Enum):
    """Financial product categories."""

    BOND = "bond"
    FUND = "fund"
    EQUITY = "equity"
    STRUCTURED = "structured"
    ABS = "abs"
    OTHER = "other"


class Product(GraphNode):
    """A financial product issued by an issuer."""

    label: str = "Product"

    product_type: ProductType = ProductType.OTHER
    issuer_id: str | None = Field(default=None, max_length=128)
    amount: float | None = Field(default=None, ge=0)
    issue_date: date | None = None
    maturity_date: date | None = None
    coupon_rate: float | None = None
    rating: str | None = Field(default=None, max_length=64)
    external_refs: dict[str, str] = Field(default_factory=dict)


class GraphEntityType(str, Enum):
    """Discriminator used by the generic ingestion and API layers."""

    COMPANY = "company"
    PERSON = "person"
    EVENT = "event"
    PRODUCT = "product"


ENTITY_TYPE_TO_MODEL: dict[GraphEntityType, type[GraphNode]] = {
    GraphEntityType.COMPANY: Company,
    GraphEntityType.PERSON: Person,
    GraphEntityType.EVENT: Event,
    GraphEntityType.PRODUCT: Product,
}

ENTITY_TYPE_TO_LABEL: dict[GraphEntityType, str] = {
    GraphEntityType.COMPANY: "Company",
    GraphEntityType.PERSON: "Person",
    GraphEntityType.EVENT: "Event",
    GraphEntityType.PRODUCT: "Product",
}

LABEL_TO_ENTITY_TYPE: dict[str, GraphEntityType] = {
    label: entity_type for entity_type, label in ENTITY_TYPE_TO_LABEL.items()
}


def node_to_properties(node: GraphNode) -> dict[str, Any]:
    """Flatten a node into Neo4j-safe primitives.

    Property names are converted to ``camelCase`` — the convention used by
    the Cypher queries, by the API responses and by the in-memory store. A
    single naming convention across all three is what keeps the offline
    repository and the live Neo4j behaviourally identical.

    ``None`` values are dropped because Neo4j cannot store them, and
    ``datetime``/``date``/``Enum`` values are converted to primitives.
    """
    payload = node.model_dump(exclude={"provenance", "label"}, mode="json")
    payload.pop("created_at", None)
    payload.pop("updated_at", None)
    properties: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        properties[_to_camel_case(key)] = value
    properties["createdAt"] = node.created_at.isoformat()
    properties["updatedAt"] = node.updated_at.isoformat()
    properties["riskLevel"] = getattr(node, "risk_level", RiskLevel.LOW).value
    properties["riskScore"] = getattr(node, "risk_score", 0.0)
    properties["provenance"] = _dump_provenance(node.provenance)
    return properties


def _dump_provenance(items: list[Provenance]) -> str:
    """Serialise the provenance list.

    Neo4j cannot store a list of maps as a property value, so the provenance
    is stored as a JSON string. It is a list, not a single record, because
    the same fact may be corroborated by several sources.
    """
    import json

    return json.dumps([item.to_properties() for item in items], ensure_ascii=False)


def _to_camel_case(name: str) -> str:
    """Convert ``snake_case`` to ``camelCase``."""
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


__all__ = [
    "ENTITY_TYPE_TO_LABEL",
    "ENTITY_TYPE_TO_MODEL",
    "LABEL_TO_ENTITY_TYPE",
    "Company",
    "CompanyStatus",
    "Event",
    "EventType",
    "GraphEntityType",
    "GraphNode",
    "Industry",
    "Person",
    "PersonRole",
    "Product",
    "ProductType",
    "RiskLevel",
    "Severity",
    "node_to_properties",
]

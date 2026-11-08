"""Domain layer: entities, relations, indicators, risk objects and provenance.

The domain layer is pure Pydantic. It has no dependency on the graph store,
the API framework or any I/O library, which keeps the business rules testable
in isolation.
"""

from fin_kg.domain.entities import (
    Company,
    CompanyStatus,
    ENTITY_TYPE_TO_LABEL,
    ENTITY_TYPE_TO_MODEL,
    Event,
    EventType,
    GraphEntityType,
    GraphNode,
    Industry,
    LABEL_TO_ENTITY_TYPE,
    Person,
    PersonRole,
    Product,
    ProductType,
    RiskLevel,
    Severity,
    node_to_properties,
)
from fin_kg.domain.indicators import (
    INDICATOR_BY_ID,
    RISK_INDICATORS,
    IndicatorCategory,
    IndicatorDirection,
    IndicatorSpec,
    indicators_by_category,
    validate_indicator_weights,
)
from fin_kg.domain.provenance import ExtractionMethod, Provenance, SourceType, utcnow
from fin_kg.domain.relations import (
    RELATION_SCHEMA,
    SYMMETRIC_RELATIONS,
    Relation,
    RelationType,
)
from fin_kg.domain.risk import (
    AlertSeverity,
    AlertStatus,
    IndicatorReading,
    RiskAlert,
    RiskAssessment,
    RiskScore,
    TransmissionPath,
)

__all__ = [
    "AlertSeverity",
    "AlertStatus",
    "Company",
    "CompanyStatus",
    "ENTITY_TYPE_TO_LABEL",
    "ENTITY_TYPE_TO_MODEL",
    "Event",
    "EventType",
    "ExtractionMethod",
    "GraphEntityType",
    "GraphNode",
    "INDICATOR_BY_ID",
    "IndicatorCategory",
    "IndicatorDirection",
    "IndicatorReading",
    "IndicatorSpec",
    "Industry",
    "LABEL_TO_ENTITY_TYPE",
    "Person",
    "PersonRole",
    "Provenance",
    "Product",
    "ProductType",
    "RELATION_SCHEMA",
    "RISK_INDICATORS",
    "Relation",
    "RelationType",
    "RiskAlert",
    "RiskAssessment",
    "RiskLevel",
    "RiskScore",
    "SYMMETRIC_RELATIONS",
    "Severity",
    "SourceType",
    "TransmissionPath",
    "indicators_by_category",
    "node_to_properties",
    "utcnow",
    "validate_indicator_weights",
]

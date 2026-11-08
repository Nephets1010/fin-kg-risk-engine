"""Relationship type definitions.

Edges are typed and carry their own provenance plus edge-specific attributes.
Two properties deserve attention:

``percent``
    Ownership ratio for ``HOLD``. Required, in ``(0, 1]``.

``transmissibility``
    How strongly risk flows along this edge, in ``[0, 1]``. Ownership and
    guarantee chains transmit strongly; a weak ``RELATED`` edge barely
    transmits at all. This single attribute drives the propagation engine and
    is what makes a "担保链穿透" explainable rather than a black box.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fin_kg.domain.provenance import Provenance, utcnow


class RelationType(str, Enum):
    """Typed edge kinds in the graph."""

    HOLD = "HOLD"                      # 持股
    LEGAL_REP = "LEGAL_REP"            # 法定代表人
    GUARANTEE = "GUARANTEE"            # 担保
    RELATED = "RELATED"                # 关联
    INVOLVED_IN = "INVOLVED_IN"        # 涉诉
    ISSUE = "ISSUE"                    # 发行
    DIRECTOR_OF = "DIRECTOR_OF"        # 任职董事
    SHAREHOLDER_OF = "SHAREHOLDER_OF"  # 股东
    CONTROLS = "CONTROLS"              # 控制
    EMPLOYEE_OF = "EMPLOYEE_OF"        # 高管任职
    SUPPLIER_OF = "SUPPLIER_OF"        # 供应商
    CUSTOMER_OF = "CUSTOMER_OF"        # 客户
    INVESTS_IN = "INVESTS_IN"          # 投资
    MERGER_OF = "MERGER_OF"            # 合并
    COUNTERPARTY_OF = "COUNTERPARTY_OF"  # 交易对手
    INVOLVED_IN_PRODUCT = "INVOLVED_IN_PRODUCT"  # 涉事产品


#: Directionality of each relation type, expressed as the set of labels the
#: edge may point from / to. ``None`` means "any node type".
RELATION_SCHEMA: dict[RelationType, dict[str, Any]] = {
    RelationType.HOLD: {
        "from": "Company",
        "to": "Company",
        "required": {"percent"},
        "optional": {"since", "until", "shareholding_type"},
        "transmissibility": 0.9,
        "description": "股权持有关系，percent 为持股比例",
    },
    RelationType.LEGAL_REP: {
        "from": "Person",
        "to": "Company",
        "required": set(),
        "optional": {"since"},
        "transmissibility": 0.8,
        "description": "法定代表人关系",
    },
    RelationType.GUARANTEE: {
        "from": "Company",
        "to": "Company",
        "required": {"guaranteed_amount"},
        "optional": {"since", "until", "guarantee_type"},
        "transmissibility": 1.0,
        "description": "担保关系，是最强的风险传导通道",
    },
    RelationType.RELATED: {
        "from": "Company",
        "to": "Company",
        "required": set(),
        "optional": {"relation_type", "detail"},
        "transmissibility": 0.3,
        "description": "一般关联关系",
    },
    RelationType.INVOLVED_IN: {
        "from": "Company",
        "to": "Event",
        "required": set(),
        "optional": {"role", "amount"},
        "transmissibility": 0.0,
        "description": "主体涉诉或涉事关系",
    },
    RelationType.ISSUE: {
        "from": "Company",
        "to": "Product",
        "required": set(),
        "optional": {"amount", "issue_date"},
        "transmissibility": 0.2,
        "description": "发行关系",
    },
    RelationType.DIRECTOR_OF: {
        "from": "Person",
        "to": "Company",
        "required": set(),
        "optional": {"since", "title"},
        "transmissibility": 0.7,
        "description": "董事任职关系",
    },
    RelationType.SHAREHOLDER_OF: {
        "from": "Person",
        "to": "Company",
        "required": {"percent"},
        "optional": {"since"},
        "transmissibility": 0.85,
        "description": "自然人股东持股关系",
    },
    RelationType.CONTROLS: {
        "from": "Company",
        "to": "Company",
        "required": set(),
        "optional": {"control_type"},
        "transmissibility": 0.95,
        "description": "控制关系，通常由实际控制人或集团认定",
    },
    RelationType.EMPLOYEE_OF: {
        "from": "Person",
        "to": "Company",
        "required": set(),
        "optional": {"since", "title"},
        "transmissibility": 0.5,
        "description": "高管任职关系",
    },
    RelationType.SUPPLIER_OF: {
        "from": "Company",
        "to": "Company",
        "required": set(),
        "optional": {"amount", "since"},
        "transmissibility": 0.4,
        "description": "供应关系",
    },
    RelationType.CUSTOMER_OF: {
        "from": "Company",
        "to": "Company",
        "required": set(),
        "optional": {"amount", "since"},
        "transmissibility": 0.4,
        "description": "客户关系",
    },
    RelationType.INVESTS_IN: {
        "from": "Company",
        "to": "Company",
        "required": set(),
        "optional": {"amount", "since", "round"},
        "transmissibility": 0.6,
        "description": "投资关系",
    },
    RelationType.MERGER_OF: {
        "from": "Company",
        "to": "Company",
        "required": set(),
        "optional": {"since", "ratio"},
        "transmissibility": 0.75,
        "description": "合并或吸收合并关系",
    },
    RelationType.COUNTERPARTY_OF: {
        "from": "Company",
        "to": "Company",
        "required": set(),
        "optional": {"amount", "since"},
        "transmissibility": 0.5,
        "description": "交易对手关系",
    },
    RelationType.INVOLVED_IN_PRODUCT: {
        "from": "Event",
        "to": "Product",
        "required": set(),
        "optional": {},
        "transmissibility": 0.0,
        "description": "事件涉及的产品",
    },
}

#: Relations whose direction may be flipped during ingestion without losing
#: meaning. The ingestion layer normalises them so the graph does not contain
#: both ``A SUPPLIER_OF B`` and ``B SUPPLIER_OF A``.
SYMMETRIC_RELATIONS: frozenset[RelationType] = frozenset(
    {
        RelationType.RELATED,
        RelationType.INVOLVED_IN,
    }
)


class Relation(BaseModel):
    """A typed edge between two nodes."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    source_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)
    relation_type: RelationType
    properties: dict[str, Any] = Field(default_factory=dict)
    provenance: list[Provenance] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())

    @model_validator(mode="after")
    def _validate_required_properties(self) -> "Relation":
        schema = RELATION_SCHEMA[self.relation_type]
        missing = schema["required"] - set(self.properties)
        if missing:
            raise ValueError(
                f"relation {self.relation_type.value} requires properties: {sorted(missing)}"
            )
        if self.source_id == self.target_id:
            raise ValueError("self-loop relations are not allowed")
        return self

    @property
    def transmissibility(self) -> float:
        """Default transmission strength, overridable per edge."""
        override = self.properties.get("transmissibility")
        if isinstance(override, (int, float)):
            return max(0.0, min(1.0, float(override)))
        return float(RELATION_SCHEMA[self.relation_type]["transmissibility"])

    def to_properties(self) -> dict[str, Any]:
        """Flatten into Neo4j-safe primitives."""
        properties: dict[str, Any] = {}
        for key, value in self.properties.items():
            if value is None:
                continue
            if isinstance(value, date):
                properties[key] = value.isoformat()
            else:
                properties[key] = value
        properties["relationType"] = self.relation_type.value
        properties["transmissibility"] = self.transmissibility
        properties["createdAt"] = self.created_at
        properties["provenance"] = [p.to_properties() for p in self.provenance]
        return properties


__all__ = [
    "RELATION_SCHEMA",
    "SYMMETRIC_RELATIONS",
    "Relation",
    "RelationType",
]

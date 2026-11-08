"""Risk indicator catalogue.

A *risk indicator* is a named, versioned, auditable computation over graph
state. Each indicator declares:

* the node label and edge types it reads,
* the requirement vector it evaluates,
* how the raw measurement maps onto a 0-100 sub-score,
* its weight inside the composite risk score,
* the evidence it needs to be reproduced (which is what makes a risk alert
  explainable rather than a black box).

Indicators are declared in :data:`RISK_INDICATORS` and are the *only* source
of truth for the baseline score model. Rules in ``rules/risk_rules.yaml``
compose indicators into alert conditions.

Indicator versioning matters for auditability: every alert records the
``version`` of each indicator that contributed to it, so a score computed
today can be compared with one computed six months ago.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class IndicatorDirection(str, Enum):
    """Whether a larger measurement means more or less risk."""

    HIGHER_IS_RISKIER = "higher_is_risker"
    LOWER_IS_RISKIER = "lower_is_risker"


class IndicatorCategory(str, Enum):
    """Risk dimensions used to group indicators in the reporting views."""

    LEGAL = "legal"  # 司法与合规
    FINANCIAL = "financial"  # 财务与偿付
    GOVERNANCE = "governance"  # 治理与关联
    MARKET = "market"  # 市场与舆情
    CHAIN = "chain"  # 传导与集中度


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """Static declaration of one risk indicator."""

    id: str
    name: str
    category: IndicatorCategory
    version: str
    direction: IndicatorDirection
    weight: float
    unit: str
    description: str
    #: Cypher snippet computing the raw measurement.
    measurement_cypher: str
    #: ``(threshold, score)`` pairs mapping the measurement onto 0-100.
    thresholds: tuple[tuple[float, float], ...] = ()
    #: Edge types the indicator traverses.
    traverses: tuple[str, ...] = ()
    #: Minimum observations required for the indicator to be trusted.
    min_observations: int = 1
    #: Optional custom evaluator; when absent the generic scorer is used.
    evaluator: Callable[[dict[str, float]], float] | None = field(default=None, repr=False)

    def score(self, measurement: float | None) -> float:
        """Map a raw measurement onto a 0-100 sub-score.

        For ``HIGHER_IS_RISKIER`` the thresholds are ascending and the score
        is the mapped value of the last threshold the measurement reaches.

        For ``LOWER_IS_RISKIER`` the semantics invert: the score is the mapped
        value of the *first* threshold the measurement falls **below**. A
        measurement above every threshold scores 0.

        Returns ``0.0`` when the measurement is missing.
        """
        if measurement is None:
            return 0.0
        if not self.thresholds:
            return 0.0

        if self.direction is IndicatorDirection.HIGHER_IS_RISKIER:
            score = 0.0
            for threshold, mapped in self.thresholds:
                if measurement >= threshold:
                    score = mapped
                else:
                    break
            return max(0.0, min(100.0, score))

        for threshold, mapped in self.thresholds:
            if measurement <= threshold:
                return max(0.0, min(100.0, mapped))
        return 0.0


#: The catalogue. Weights sum to 1.0 — :func:`validate_indicator_weights`
#: enforces this at import time in the test-suite.
RISK_INDICATORS: tuple[IndicatorSpec, ...] = (
    IndicatorSpec(
        id="IND-LIT-001",
        name="涉诉事件数",
        category=IndicatorCategory.LEGAL,
        version="1.0.0",
        direction=IndicatorDirection.HIGHER_IS_RISKIER,
        weight=0.15,
        unit="次",
        description="近 12 个月内主体作为被告或被强制执行的诉讼/执行事件数量",
        measurement_cypher=(
            "MATCH (c:Company {id:$id})-[:INVOLVED_IN]->(e:Event) "
            "WHERE e.eventType IN ['litigation','enforcement'] "
            "AND e.occurredDate >= date($since) RETURN count(e) AS value"
        ),
        thresholds=((0, 0.0), (1, 25.0), (3, 55.0), (10, 85.0), (30, 100.0)),
    ),
    IndicatorSpec(
        id="IND-ENF-001",
        name="强制执行金额占注册资本比",
        category=IndicatorCategory.LEGAL,
        version="1.1.0",
        direction=IndicatorDirection.HIGHER_IS_RISKIER,
        weight=0.15,
        unit="倍",
        description=(
            "近 12 个月强制执行标的金额相对注册资本的倍数。"
            "使用比率而非绝对金额，是为了让不同规模的主体可比"
        ),
        measurement_cypher=(
            "MATCH (c:Company {id:$id})-[:INVOLVED_IN]->(e:Event) "
            "WHERE e.eventType='enforcement' AND e.occurredDate >= date($since) "
            "WITH c, coalesce(sum(e.amount),0) AS exposure "
            "RETURN CASE WHEN c.regCapital > 0 "
            "THEN exposure / c.regCapital ELSE 0 END AS value"
        ),
        thresholds=((0, 0.0), (0.1, 30.0), (0.5, 60.0), (2.0, 85.0), (5.0, 100.0)),
    ),
    IndicatorSpec(
        id="IND-PEN-001",
        name="行政处罚次数",
        category=IndicatorCategory.LEGAL,
        version="1.0.0",
        direction=IndicatorDirection.HIGHER_IS_RISKIER,
        weight=0.10,
        unit="次",
        description="近 12 个月行政处罚事件数量",
        measurement_cypher=(
            "MATCH (c:Company {id:$id})-[:INVOLVED_IN]->(e:Event) "
            "WHERE e.eventType='penalty' AND e.occurredDate >= date($since) "
            "RETURN count(e) AS value"
        ),
        thresholds=((0, 0.0), (1, 35.0), (3, 70.0), (6, 100.0)),
    ),
    IndicatorSpec(
        id="IND-GUA-001",
        name="对外担保余额占注册资本比",
        category=IndicatorCategory.FINANCIAL,
        version="1.1.0",
        direction=IndicatorDirection.HIGHER_IS_RISKIER,
        weight=0.20,
        unit="倍",
        description=(
            "对外担保余额相对注册资本的倍数。倍数越高代表代偿压力越大。"
            "使用比率而非绝对金额，是为了让不同规模的主体可比"
        ),
        measurement_cypher=(
            "MATCH (c:Company {id:$id})-[g:GUARANTEE]->(t:Company) "
            "WITH c, coalesce(sum(g.guaranteed_amount),0) AS exposure "
            "RETURN CASE WHEN c.regCapital > 0 "
            "THEN exposure / c.regCapital ELSE 0 END AS value"
        ),
        thresholds=((0, 0.0), (0.5, 25.0), (1.0, 50.0), (2.0, 80.0), (5.0, 100.0)),
    ),
    IndicatorSpec(
        id="IND-CHN-001",
        name="担保链深度",
        category=IndicatorCategory.CHAIN,
        version="1.0.0",
        direction=IndicatorDirection.HIGHER_IS_RISKIER,
        weight=0.10,
        unit="跳",
        description="以本主体为起点的担保链最长跳数",
        measurement_cypher=(
            "MATCH p=(c:Company {id:$id})-[:GUARANTEE*1..5]->(t) RETURN max(length(p)) AS value"
        ),
        thresholds=((0, 0.0), (1, 20.0), (2, 45.0), (3, 70.0), (5, 100.0)),
    ),
    IndicatorSpec(
        id="IND-HLD-001",
        name="股权集中度",
        category=IndicatorCategory.GOVERNANCE,
        version="1.0.0",
        direction=IndicatorDirection.LOWER_IS_RISKIER,
        weight=0.10,
        unit="比率",
        description="第一大股东持股比例，过于分散同样构成治理风险",
        measurement_cypher=(
            "MATCH (c:Company {id:$id})<-[h:HOLD]-(s:Company) "
            "RETURN coalesce(max(h.percent),0) AS value"
        ),
        thresholds=((0.0, 60.0), (0.1, 40.0), (0.2, 20.0), (0.35, 5.0)),
    ),
    IndicatorSpec(
        id="IND-CTL-001",
        name="实际控制人风险",
        category=IndicatorCategory.GOVERNANCE,
        version="1.0.0",
        direction=IndicatorDirection.HIGHER_IS_RISKIER,
        weight=0.05,
        unit="分",
        description="实际控制人自身的风险分，向下传导的治理风险",
        measurement_cypher=(
            "MATCH (p:Person)-[:LEGAL_REP]->(c:Company {id:$id}) "
            "RETURN coalesce(max(p.riskScore),0) AS value"
        ),
        thresholds=((0, 0.0), (30, 40.0), (60, 75.0), (85, 100.0)),
    ),
    IndicatorSpec(
        id="IND-RTE-001",
        name="评级下调次数",
        category=IndicatorCategory.MARKET,
        version="1.0.0",
        direction=IndicatorDirection.HIGHER_IS_RISKIER,
        weight=0.05,
        unit="次",
        description="近 12 个月主体信用评级下调次数",
        measurement_cypher=(
            "MATCH (c:Company {id:$id})-[:INVOLVED_IN]->(e:Event) "
            "WHERE e.eventType='rating_downgrade' AND e.occurredDate >= date($since) "
            "RETURN count(e) AS value"
        ),
        thresholds=((0, 0.0), (1, 50.0), (2, 80.0), (4, 100.0)),
    ),
    IndicatorSpec(
        id="IND-NEG-001",
        name="负面舆情热度",
        category=IndicatorCategory.MARKET,
        version="1.0.0",
        direction=IndicatorDirection.HIGHER_IS_RISKIER,
        weight=0.05,
        unit="条",
        description="近 30 天负面舆情事件数量",
        measurement_cypher=(
            "MATCH (c:Company {id:$id})-[:INVOLVED_IN]->(e:Event) "
            "WHERE e.eventType='negative_news' AND e.occurredDate >= date($since) "
            "RETURN count(e) AS value"
        ),
        thresholds=((0, 0.0), (3, 40.0), (10, 70.0), (25, 100.0)),
    ),
    IndicatorSpec(
        id="IND-DEF-001",
        name="违约事件数",
        category=IndicatorCategory.FINANCIAL,
        version="1.0.0",
        direction=IndicatorDirection.HIGHER_IS_RISKIER,
        weight=0.05,
        unit="次",
        description="近 12 个月违约事件数量",
        measurement_cypher=(
            "MATCH (c:Company {id:$id})-[:INVOLVED_IN]->(e:Event) "
            "WHERE e.eventType='default' AND e.occurredDate >= date($since) "
            "RETURN count(e) AS value"
        ),
        thresholds=((0, 0.0), (1, 70.0), (2, 90.0), (4, 100.0)),
    ),
)

INDICATOR_BY_ID: dict[str, IndicatorSpec] = {spec.id: spec for spec in RISK_INDICATORS}


def validate_indicator_weights(specs: tuple[IndicatorSpec, ...] | None = None) -> bool:
    """Return ``True`` when the weights of the catalogue sum to 1.0 (±0.01)."""
    catalogue = specs if specs is not None else RISK_INDICATORS
    total = sum(spec.weight for spec in catalogue)
    return abs(total - 1.0) <= 0.01


def indicators_by_category(category: IndicatorCategory) -> tuple[IndicatorSpec, ...]:
    """Return every indicator belonging to a category."""
    return tuple(spec for spec in RISK_INDICATORS if spec.category is category)


__all__ = [
    "INDICATOR_BY_ID",
    "RISK_INDICATORS",
    "IndicatorCategory",
    "IndicatorDirection",
    "IndicatorSpec",
    "indicators_by_category",
    "validate_indicator_weights",
]

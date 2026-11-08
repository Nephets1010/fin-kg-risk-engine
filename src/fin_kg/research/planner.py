"""Natural-language question understanding for the KGQA module.

The module turns a Chinese question into a structured query plan. The plan is
then either executed as Cypher (against Neo4j) or evaluated directly against
the in-memory snapshot, and the answer is assembled from the result together
with the evidence path.

The design is deliberately *intent-based* rather than free-form text-to-Cypher.
A question is classified into one of a small number of intents, and each
intent has a parameter extractor and a query template. The reasons:

* A generated Cypher string that fails at runtime is useless to an analyst.
* A template cannot produce a destructive query, because the templates are
  the only queries that can ever be produced.
* The intent is auditable: "this question was answered by the
  ``guarantee_chain`` intent with entity X" is a record an analyst can check.

When an LLM backend is configured, it is used to *disambiguate* the intent and
to phrase the final answer — never to execute arbitrary Cypher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fin_kg.core.errors import ValidationError
from fin_kg.core.logging import get_logger

logger = get_logger(__name__)


class QueryIntent(str, Enum):
    """The supported question types."""

    ENTITY_PROFILE = "entity_profile"  # 公司概况
    HOLDING_STRUCTURE = "holding_structure"  # 股权结构
    GUARANTEE_CHAIN = "guarantee_chain"  # 担保链
    RISK_ASSESSMENT = "risk_assessment"  # 风险评估
    EVENT_HISTORY = "event_history"  # 事件历史
    RELATED_PARTIES = "related_parties"  # 关联企业
    TOP_RISKED = "top_risked"  # 高风险主体
    EXPOSURE = "exposure"  # 风险敞口
    UNKNOWN = "unknown"


@dataclass(slots=True)
class QueryPlan:
    """The structured interpretation of one question."""

    intent: QueryIntent
    entity_name: str | None = None
    entity_id: str | None = None
    hops: int = 3
    limit: int = 20
    confidence: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "entityName": self.entity_name,
            "entityId": self.entity_id,
            "hops": self.hops,
            "limit": self.limit,
            "confidence": round(self.confidence, 3),
            "matchedKeywords": list(self.matched_keywords),
        }


#: Keyword table per intent, ordered by specificity.
_INTENT_KEYWORDS: dict[QueryIntent, tuple[str, ...]] = {
    QueryIntent.GUARANTEE_CHAIN: ("担保链", "担保穿透", "代偿链", "保证链"),
    QueryIntent.HOLDING_STRUCTURE: ("股权结构", "股东", "持股", "谁持有", "实际控制"),
    QueryIntent.RISK_ASSESSMENT: ("风险", "风险分", "评级", "安全吗", "危险", "评分"),
    QueryIntent.EVENT_HISTORY: ("事件", "诉讼", "处罚", "被执行", "舆情", "违约", "记录"),
    QueryIntent.RELATED_PARTIES: ("关联", "关系", "子公司", "母子公司", "对外投资"),
    QueryIntent.EXPOSURE: ("敞口", "暴露", "受影响", "谁受影响", "传导"),
    QueryIntent.TOP_RISKED: ("最高风险", "风险最高", "排名", "最危险", "top"),
    QueryIntent.ENTITY_PROFILE: ("概况", "介绍", "信息", "资料", "是什么", "工商"),
}

#: Patterns that extract an entity name from the question.
_ENTITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^关于\s*(?P<name>[一-龥A-Za-z0-9（）()·\-]{2,40}?)\s*的"),
    re.compile(r"^(?P<name>[一-龥A-Za-z0-9（）()·\-]{2,40}?)\s*的"),
    re.compile(r"(?P<name>[一-龥A-Za-z0-9（）()·\-]{2,40}?)\s*(?:公司|集团|企业|科技|控股|银行)"),
    re.compile(r"([一-龥]{2,20}(?:有限公司|股份有限公司|集团|银行|科技))"),
)

_NUMBER_RE = re.compile(r"(\d+)\s*跳")
_LIMIT_RE = re.compile(r"前\s*(\d+)\s*(?:名|个|家)")


def extract_entity_name(question: str) -> str | None:
    """Pull an entity name out of a Chinese question."""
    text = question.strip()
    for pattern in _ENTITY_PATTERNS:
        match = pattern.search(text)
        if match:
            candidate = match.group("name").strip()
            if candidate:
                return candidate
    return None


#: Tie-break weights. When two intents match equally well, the one with the
#: higher weight wins. The weights encode how *specific* an intent is:
#: "敞口" (exposure) is a more deliberate term than a bare "风险" (risk), so
#: a question mentioning both is an exposure question.
_INTENT_PRIORITY: dict[QueryIntent, int] = {
    QueryIntent.GUARANTEE_CHAIN: 30,
    QueryIntent.EXPOSURE: 25,
    QueryIntent.HOLDING_STRUCTURE: 20,
    QueryIntent.EVENT_HISTORY: 20,
    QueryIntent.RELATED_PARTIES: 20,
    QueryIntent.TOP_RISKED: 20,
    QueryIntent.RISK_ASSESSMENT: 10,
    QueryIntent.ENTITY_PROFILE: 5,
    QueryIntent.UNKNOWN: 0,
}


def classify_intent(question: str) -> tuple[QueryIntent, list[str]]:
    """Classify a question into one of the supported intents.

    Scoring is by *total matched keyword length*, not by keyword count: a
    single specific keyword such as ``担保链`` must outweigh a generic
    ``风险`` that happens to appear in ``风险最高的前 5 家公司``. Ties are
    broken by :data:`_INTENT_PRIORITY`.
    """
    text = question.strip()
    scores: dict[QueryIntent, int] = {}
    matched: dict[QueryIntent, list[str]] = {}

    for intent, keywords in _INTENT_KEYWORDS.items():
        hits = [keyword for keyword in keywords if keyword in text]
        if hits:
            scores[intent] = sum(len(keyword) for keyword in hits)
            matched[intent] = hits

    if not scores:
        return QueryIntent.UNKNOWN, []

    best = max(
        scores,
        key=lambda intent: (scores[intent], _INTENT_PRIORITY.get(intent, 0)),
    )
    return best, matched[best]


class QueryPlanner:
    """Turns a question into a :class:`QueryPlan`."""

    def __init__(self, *, default_hops: int = 3, default_limit: int = 20) -> None:
        self.default_hops = default_hops
        self.default_limit = default_limit

    def plan(self, question: str) -> QueryPlan:
        """Build the plan for a question."""
        if not question or not question.strip():
            raise ValidationError("question must not be empty")

        intent, keywords = classify_intent(question)
        entity_name = extract_entity_name(question)
        hops = self.default_hops
        limit = self.default_limit

        hop_match = _NUMBER_RE.search(question)
        if hop_match:
            hops = max(1, min(10, int(hop_match.group(1))))
        limit_match = _LIMIT_RE.search(question)
        if limit_match:
            limit = max(1, min(500, int(limit_match.group(1))))

        confidence = 0.9 if intent is not QueryIntent.UNKNOWN else 0.2
        if entity_name is None and intent not in {
            QueryIntent.TOP_RISKED,
            QueryIntent.UNKNOWN,
        }:
            confidence -= 0.3

        plan = QueryPlan(
            intent=intent,
            entity_name=entity_name,
            hops=hops,
            limit=limit,
            confidence=max(0.0, confidence),
            matched_keywords=keywords,
        )
        logger.debug("query plan: %s", plan.as_dict())
        return plan


__all__ = [
    "QueryIntent",
    "QueryPlan",
    "QueryPlanner",
    "classify_intent",
    "extract_entity_name",
]

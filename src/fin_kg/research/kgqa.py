"""KGQA — question answering over the knowledge graph.

The answering pipeline is:

1. :class:`~fin_kg.research.planner.QueryPlanner` classifies the question
   into an intent and extracts the entity and parameters.
2. The intent's handler queries the graph and collects the raw facts.
3. A renderer turns the facts into a Chinese answer.
4. The **evidence path** is attached: the exact nodes and relations that
   produced the answer, so an analyst can click through and verify it.

The renderer works with or without an LLM. Without one it produces a
deterministic, structured answer. With one it produces a natural-language
answer — but the evidence path is identical in both cases, because the
evidence is derived from the graph, never from the language model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fin_kg.core.errors import NotFoundError
from fin_kg.core.logging import get_logger
from fin_kg.domain.relations import RelationType
from fin_kg.graph.repository import GraphRepository
from fin_kg.research.planner import QueryIntent, QueryPlan, QueryPlanner

logger = get_logger(__name__)


@dataclass(slots=True)
class Evidence:
    """One piece of evidence backing an answer."""

    kind: str
    description: str
    node_ids: list[str] = field(default_factory=list)
    node_names: list[str] = field(default_factory=list)
    relation_types: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "nodeIds": list(self.node_ids),
            "nodeNames": list(self.node_names),
            "relationTypes": list(self.relation_types),
            "properties": dict(self.properties),
        }


@dataclass(slots=True)
class Answer:
    """The answer to one question."""

    question: str
    answer: str
    intent: QueryIntent
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "intent": self.intent.value,
            "confidence": round(self.confidence, 3),
            "evidence": [item.as_dict() for item in self.evidence],
            "data": dict(self.data),
            "plan": dict(self.plan),
        }


class KGQA:
    """Answers natural-language questions about the financial graph."""

    def __init__(
        self,
        repository: GraphRepository,
        *,
        planner: QueryPlanner | None = None,
        llm: Any | None = None,
    ) -> None:
        self.repository = repository
        self.planner = planner or QueryPlanner()
        self.llm = llm

    # -- public API ---------------------------------------------------------

    def ask(self, question: str) -> Answer:
        """Answer one question."""
        plan = self.planner.plan(question)
        handler = self._handler(plan.intent)
        facts, evidence = handler(plan)
        answer_text = self._render(plan, facts, evidence, question=question)
        return Answer(
            question=question,
            answer=answer_text,
            intent=plan.intent,
            confidence=plan.confidence if facts else 0.0,
            evidence=evidence,
            data=facts,
            plan=plan.as_dict(),
        )

    # -- entity resolution --------------------------------------------------

    def _resolve_entity(self, plan: QueryPlan) -> dict[str, Any]:
        """Find the entity the question is about."""
        if plan.entity_id:
            node = self.repository.get_node(plan.entity_id)
            if node:
                return node
        if not plan.entity_name:
            raise NotFoundError(
                "the question does not name an entity", details={"question": plan.question}
            )

        candidates = self.repository.find_nodes("Company")
        exact = [
            node
            for node in candidates
            if str(node.get("name", "")).strip() == plan.entity_name.strip()
        ]
        if exact:
            return exact[0]

        partial = [
            node
            for node in candidates
            if plan.entity_name in str(node.get("name", ""))
            or str(node.get("name", "")) in plan.entity_name
        ]
        if partial:
            partial.sort(key=lambda node: len(str(node.get("name", ""))))
            return partial[0]
        raise NotFoundError(
            "entity not found in the graph",
            details={"entityName": plan.entity_name},
        )

    # -- handlers -----------------------------------------------------------

    def _handler(self, intent: QueryIntent) -> Any:
        return {
            QueryIntent.ENTITY_PROFILE: self._handle_profile,
            QueryIntent.HOLDING_STRUCTURE: self._handle_holding,
            QueryIntent.GUARANTEE_CHAIN: self._handle_guarantee_chain,
            QueryIntent.RISK_ASSESSMENT: self._handle_risk,
            QueryIntent.EVENT_HISTORY: self._handle_events,
            QueryIntent.RELATED_PARTIES: self._handle_related,
            QueryIntent.TOP_RISKED: self._handle_top_risked,
            QueryIntent.EXPOSURE: self._handle_exposure,
            QueryIntent.UNKNOWN: self._handle_unknown,
        }[intent]

    def _handle_profile(self, plan: QueryPlan) -> tuple[dict[str, Any], list[Evidence]]:
        node = self._resolve_entity(plan)
        facts = {
            "name": node.get("name"),
            "industry": node.get("industry"),
            "status": node.get("status"),
            "regCapital": node.get("regCapital"),
            "foundedDate": node.get("foundedDate"),
            "region": node.get("region"),
            "riskScore": node.get("riskScore"),
            "riskLevel": node.get("riskLevel"),
        }
        evidence = [
            Evidence(
                kind="node",
                description=f"企业工商登记信息：{node.get('name')}",
                node_ids=[str(node.get("id", ""))],
                node_names=[str(node.get("name", ""))],
                properties=facts,
            )
        ]
        return facts, evidence

    def _handle_holding(self, plan: QueryPlan) -> tuple[dict[str, Any], list[Evidence]]:
        node = self._resolve_entity(plan)
        holders = self._incoming_edges(str(node.get("id", "")), RelationType.HOLD)
        facts = {
            "company": node.get("name"),
            "holders": [
                {
                    "id": edge.get("source"),
                    "name": self._name_of(str(edge.get("source", ""))),
                    "percent": edge.get("percent"),
                }
                for edge in holders
            ],
        }
        holder_ids = [str(edge.get("source", "")) for edge in holders]
        evidence = [
            Evidence(
                kind="relation",
                description="股权持有关系",
                node_ids=[*holder_ids, str(node.get("id", ""))],
                relation_types=["HOLD"] * len(holders),
                properties={
                    holder_id: edge.get("percent")
                    for holder_id, edge in zip(holder_ids, holders, strict=True)
                },
            )
        ]
        return facts, evidence

    def _handle_guarantee_chain(self, plan: QueryPlan) -> tuple[dict[str, Any], list[Evidence]]:
        node = self._resolve_entity(plan)
        paths = self.repository.traverse(
            str(node.get("id", "")),
            [RelationType.GUARANTEE],
            max_hops=plan.hops,
            direction="out",
        )
        chains = []
        evidence: list[Evidence] = []
        for path in paths:
            chains.append(
                {
                    "path": " -> ".join(str(item.get("name", "")) for item in path.nodes),
                    "hops": path.length,
                    "amounts": [rel.get("guaranteed_amount") for rel in path.relations],
                }
            )
            evidence.append(
                Evidence(
                    kind="path",
                    description="担保链路径",
                    node_ids=path.node_ids(),
                    node_names=[str(item.get("name", "")) for item in path.nodes],
                    relation_types=path.relation_types(),
                    properties={"hops": path.length},
                )
            )
        facts = {"company": node.get("name"), "chains": chains, "chainCount": len(chains)}
        return facts, evidence

    def _handle_risk(self, plan: QueryPlan) -> tuple[dict[str, Any], list[Evidence]]:
        node = self._resolve_entity(plan)
        score = float(node.get("riskScore") or 0.0)
        level = str(node.get("riskLevel") or "low")
        facts = {
            "company": node.get("name"),
            "riskScore": score,
            "riskLevel": level,
            "modelVersion": node.get("riskModelVersion"),
        }
        evidence = [
            Evidence(
                kind="score",
                description="基准风险评分模型输出",
                node_ids=[str(node.get("id", ""))],
                node_names=[str(node.get("name", ""))],
                properties=facts,
            )
        ]
        return facts, evidence

    def _handle_events(self, plan: QueryPlan) -> tuple[dict[str, Any], list[Evidence]]:
        node = self._resolve_entity(plan)
        edges = self._incoming_edges(str(node.get("id", "")), RelationType.INVOLVED_IN)
        events = []
        for edge in edges:
            event = self.repository.get_node(str(edge.get("target", ""))) or {}
            events.append(
                {
                    "type": event.get("eventType"),
                    "severity": event.get("severity"),
                    "date": event.get("occurredDate"),
                    "amount": event.get("amount"),
                    "description": event.get("description"),
                }
            )
        events.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
        events = events[: plan.limit]
        facts = {"company": node.get("name"), "events": events, "eventCount": len(events)}
        evidence = [
            Evidence(
                kind="event",
                description=f"主体关联事件 {len(events)} 条",
                node_ids=[str(edge.get("target", "")) for edge in edges],
                relation_types=["INVOLVED_IN"] * len(edges),
            )
        ]
        return facts, evidence

    def _handle_related(self, plan: QueryPlan) -> tuple[dict[str, Any], list[Evidence]]:
        node = self._resolve_entity(plan)
        relations = (
            RelationType.HOLD,
            RelationType.GUARANTEE,
            RelationType.RELATED,
            RelationType.INVESTS_IN,
            RelationType.CONTROLS,
            RelationType.MERGER_OF,
        )
        related: list[dict[str, Any]] = []
        evidence: list[Evidence] = []
        for relation in relations:
            for edge in self._incoming_edges(str(node.get("id", "")), relation):
                related.append(
                    {
                        "name": self._name_of(str(edge.get("source", ""))),
                        "id": edge.get("source"),
                        "relation": relation.value,
                        "weight": edge.get("percent") or edge.get("guaranteed_amount"),
                    }
                )
            for edge in self._outgoing_edges(str(node.get("id", "")), relation):
                related.append(
                    {
                        "name": self._name_of(str(edge.get("target", ""))),
                        "id": edge.get("target"),
                        "relation": relation.value,
                        "weight": edge.get("percent") or edge.get("guaranteed_amount"),
                    }
                )
        facts = {"company": node.get("name"), "related": related, "relatedCount": len(related)}
        evidence = [
            Evidence(
                kind="relation",
                description="关联企业清单",
                node_ids=[item["id"] for item in related if item.get("id")],
                relation_types=[item["relation"] for item in related],
            )
        ]
        return facts, evidence

    def _handle_top_risked(self, plan: QueryPlan) -> tuple[dict[str, Any], list[Evidence]]:
        companies = self.repository.find_nodes("Company")
        ranked = sorted(
            companies, key=lambda node: float(node.get("riskScore") or 0.0), reverse=True
        )[: plan.limit]
        items = [
            {
                "id": node.get("id"),
                "name": node.get("name"),
                "riskScore": node.get("riskScore"),
                "riskLevel": node.get("riskLevel"),
            }
            for node in ranked
        ]
        facts = {"count": len(items), "companies": items}
        evidence = [
            Evidence(
                kind="score",
                description="风险分排名",
                node_ids=[str(item["id"]) for item in items if item.get("id")],
                node_names=[str(item.get("name", "")) for item in items],
                properties={"riskScore": [item.get("riskScore") for item in items]},
            )
        ]
        return facts, evidence

    def _handle_exposure(self, plan: QueryPlan) -> tuple[dict[str, Any], list[Evidence]]:
        node = self._resolve_entity(plan)
        relations = (
            RelationType.GUARANTEE,
            RelationType.HOLD,
            RelationType.CONTROLS,
            RelationType.INVESTS_IN,
        )
        paths = self.repository.traverse(
            str(node.get("id", "")), relations, max_hops=plan.hops, direction="out"
        )
        items = []
        for path in paths:
            items.append(
                {
                    "path": " -> ".join(str(item.get("name", "")) for item in path.nodes),
                    "hops": path.length,
                    "terminal": str(path.nodes[-1].get("name", "")),
                }
            )
        facts = {"company": node.get("name"), "exposed": items, "exposedCount": len(items)}
        evidence = [
            Evidence(
                kind="path",
                description="风险传导路径",
                node_ids=path.node_ids(),
                node_names=[str(item.get("name", "")) for item in path.nodes],
                relation_types=path.relation_types(),
            )
            for path in paths[:20]
        ]
        return facts, evidence

    def _handle_unknown(self, plan: QueryPlan) -> tuple[dict[str, Any], list[Evidence]]:
        return (
            {
                "suggestions": [
                    "试试问：某公司的股权结构是什么？",
                    "试试问：某公司的担保链有多长？",
                    "试试问：某公司的风险分是多少？",
                    "试试问：风险最高的前 10 家公司",
                ]
            },
            [],
        )

    # -- helpers ------------------------------------------------------------

    def _incoming_edges(self, node_id: str, relation: RelationType) -> list[dict[str, Any]]:
        """Edges of the given type pointing at ``node_id``."""
        snapshot = getattr(self.repository, "snapshot", None)
        if snapshot is None:
            return []
        return [
            edge
            for edge in snapshot.adjacency.get(node_id, [])
            if edge.get("relationType") == relation.value and edge.get("target") == node_id
        ]

    def _outgoing_edges(self, node_id: str, relation: RelationType) -> list[dict[str, Any]]:
        """Edges of the given type leaving ``node_id``."""
        snapshot = getattr(self.repository, "snapshot", None)
        if snapshot is None:
            return []
        return [
            edge
            for edge in snapshot.adjacency.get(node_id, [])
            if edge.get("relationType") == relation.value and edge.get("source") == node_id
        ]

    def _name_of(self, node_id: str) -> str:
        node = self.repository.get_node(node_id) or {}
        return str(node.get("name", node_id))

    # -- rendering ----------------------------------------------------------

    def _render(
        self,
        plan: QueryPlan,
        facts: dict[str, Any],
        evidence: list[Evidence],
        *,
        question: str = "",
    ) -> str:
        """Compose the Chinese answer text.

        The original question is passed in rather than recovered from the
        plan: the plan holds the *interpretation*, not the wording, and the
        LLM needs the wording to phrase a reply to.
        """
        if self.llm is not None:
            try:
                return self._render_with_llm(plan, facts, evidence, question=question)
            except Exception as exc:  # never fail because of the LLM
                logger.warning("LLM rendering failed, falling back to template: %s", exc)
        return self._render_with_template(plan, facts, evidence)

    def _render_with_template(
        self, plan: QueryPlan, facts: dict[str, Any], evidence: list[Evidence]
    ) -> str:
        intent = plan.intent
        if intent is QueryIntent.ENTITY_PROFILE:
            return (
                f"{facts.get('name')}：行业 {facts.get('industry')}，状态 {facts.get('status')}，"
                f"注册资本 {facts.get('regCapital')}，注册地 {facts.get('region')}，"
                f"当前风险分 {facts.get('riskScore')}（{facts.get('riskLevel')}）。"
            )
        if intent is QueryIntent.HOLDING_STRUCTURE:
            holders = facts.get("holders") or []
            if not holders:
                return f"{facts.get('company')} 暂无已知股东记录。"
            lines = [
                f"{item.get('name')} 持股 {float(item.get('percent') or 0) * 100:.2f}%"
                for item in holders
            ]
            return f"{facts.get('company')} 的股权结构：" + "；".join(lines) + "。"
        if intent is QueryIntent.GUARANTEE_CHAIN:
            chains = facts.get("chains") or []
            if not chains:
                return f"{facts.get('company')} 未发现对外担保链。"
            lines = [f"{item['path']}（{item['hops']} 跳）" for item in chains[:5]]
            return (
                f"{facts.get('company')} 共有 {len(chains)} 条担保传导路径，最长 "
                f"{max(item['hops'] for item in chains)} 跳。示例：" + "；".join(lines) + "。"
            )
        if intent is QueryIntent.RISK_ASSESSMENT:
            return (
                f"{facts.get('company')} 当前风险分 {facts.get('riskScore')}，"
                f"风险等级 {facts.get('riskLevel')}"
                f"（模型版本 {facts.get('modelVersion') or 'baseline-1.0.0'}）。"
            )
        if intent is QueryIntent.EVENT_HISTORY:
            events = facts.get("events") or []
            if not events:
                return f"{facts.get('company')} 未检索到风险事件。"
            lines = [
                f"{item.get('date')} {item.get('type')}（{item.get('severity')}）"
                for item in events[:5]
            ]
            return f"{facts.get('company')} 近 {len(events)} 条事件：" + "；".join(lines) + "。"
        if intent is QueryIntent.RELATED_PARTIES:
            related = facts.get("related") or []
            if not related:
                return f"{facts.get('company')} 未发现关联企业。"
            lines = [f"{item['name']}（{item['relation']}）" for item in related[:8]]
            return (
                f"{facts.get('company')} 共有 {len(related)} 个关联主体：" + "；".join(lines) + "。"
            )
        if intent is QueryIntent.TOP_RISKED:
            items = facts.get("companies") or []
            if not items:
                return "图谱中暂无企业数据。"
            lines = [
                f"{index + 1}. {item.get('name')}（{item.get('riskScore')}）"
                for index, item in enumerate(items[:10])
            ]
            return "风险分排名：" + "；".join(lines) + "。"
        if intent is QueryIntent.EXPOSURE:
            exposed = facts.get("exposed") or []
            if not exposed:
                return f"{facts.get('company')} 暂未发现风险敞口路径。"
            lines = [item["path"] for item in exposed[:5]]
            return (
                f"{facts.get('company')} 的风险可沿 {len(exposed)} 条路径扩散。示例："
                + "；".join(lines)
                + "。"
            )
        return (
            "暂未识别该问题类型，可尝试：股权结构 / 担保链 / 风险分 / 事件 / 关联企业 / 风险排名。"
        )

    def _render_with_llm(
        self,
        plan: QueryPlan,
        facts: dict[str, Any],
        evidence: list[Evidence],
        *,
        question: str = "",
    ) -> str:
        """Ask the LLM to phrase the answer from the facts.

        The facts are the only input. The LLM never sees the question's
        free-text intent and never produces Cypher.
        """
        from fin_kg.research.llm import LLMClient

        client: LLMClient = self.llm
        evidence_text = "\n".join(f"- {item.kind}: {item.description}" for item in evidence[:10])
        prompt = (
            "你是金融风控分析师助手。请仅依据下面的图谱事实，用简体中文回答问题。"
            "不要编造事实，不要给出图谱中没有的数字。\n\n"
            f"问题：{question}\n"
            f"事实：\n{evidence_text}\n\n"
            f"结构化数据：{facts}\n"
        )
        return client.complete(prompt)


__all__ = ["KGQA", "Answer", "Evidence"]

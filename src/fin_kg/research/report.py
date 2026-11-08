"""Research-report summarisation.

Turns the graph state of a company into a structured investment-research
briefing. The output follows the structure a research analyst would write:

1. **主体概况** — the entity and its registration facts
2. **股权结构** — who owns it
3. **风险画像** — the risk score and the indicators that drive it
4. **风险传导** — who is exposed through the guarantee chain
5. **风险事件** — the recent adverse events
6. **可比案例** — structurally similar companies
7. **关注要点** — the analyst's takeaway

The summary is deterministic and template-driven. When an LLM is configured,
it can rewrite section 7 into prose, but the numbers in sections 1-6 always
come from the graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fin_kg.core.logging import get_logger
from fin_kg.domain.risk import RiskAssessment, RiskScore
from fin_kg.graph.repository import GraphRepository
from fin_kg.research.similarity import SimilarCaseFinder

logger = get_logger(__name__)


@dataclass(slots=True)
class ReportSection:
    """One section of the briefing."""

    title: str
    content: str
    bullets: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "content": self.content,
            "bullets": list(self.bullets),
            "data": dict(self.data),
        }


@dataclass(slots=True)
class ResearchReport:
    """A complete research briefing for one entity."""

    entity_id: str
    entity_name: str
    generated_at: str
    sections: list[ReportSection] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entityId": self.entity_id,
            "entityName": self.entity_name,
            "generatedAt": self.generated_at,
            "sections": [section.as_dict() for section in self.sections],
        }

    def to_markdown(self) -> str:
        """Render the report as Markdown."""
        lines = [f"# {self.entity_name} 投研简报", ""]
        for section in self.sections:
            lines.append(f"## {section.title}")
            if section.content:
                lines.append("")
                lines.append(section.content)
            if section.bullets:
                lines.append("")
                for bullet in section.bullets:
                    lines.append(f"- {bullet}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"


class ReportGenerator:
    """Builds a research briefing from the graph and the risk assessment."""

    def __init__(
        self,
        repository: GraphRepository,
        *,
        similar_cases: SimilarCaseFinder | None = None,
    ) -> None:
        self.repository = repository
        self.similar_cases = similar_cases or SimilarCaseFinder(repository)

    def generate(
        self,
        assessment: RiskAssessment,
        *,
        today: date | None = None,
    ) -> ResearchReport:
        """Build the briefing for one assessed entity."""
        score = assessment.score
        node = self.repository.get_node(score.entity_id) or {}
        sections: list[ReportSection] = [
            self._profile_section(node),
            self._ownership_section(score.entity_id, node),
            self._risk_section(score),
            self._transmission_section(assessment),
            self._event_section(score.entity_id),
            self._comparables_section(score.entity_id),
        ]
        sections.append(self._takeaway_section(score, assessment))

        return ResearchReport(
            entity_id=score.entity_id,
            entity_name=score.entity_name or str(node.get("name", score.entity_id)),
            generated_at=(today or date.today()).isoformat(),
            sections=sections,
        )

    # -- sections -----------------------------------------------------------

    def _profile_section(self, node: dict[str, Any]) -> ReportSection:
        bullets = [
            f"行业：{node.get('industry', '未知')}",
            f"登记状态：{node.get('status', '未知')}",
            f"注册资本：{node.get('regCapital', '未知')}",
            f"成立日期：{node.get('foundedDate', '未知')}",
            f"注册地：{node.get('region', '未知')}",
        ]
        return ReportSection(
            title="主体概况",
            content=f"{node.get('name', '未知主体')} 的工商登记与基础信息。",
            bullets=bullets,
            data={
                key: node.get(key)
                for key in ("name", "industry", "status", "regCapital", "foundedDate", "region")
            },
        )

    def _ownership_section(self, entity_id: str, node: dict[str, Any]) -> ReportSection:
        holders: list[str] = []
        for edge in self.repository.neighbours(entity_id):
            if edge.get("relationType") != "HOLD" or edge.get("target") != entity_id:
                continue
            holder = self.repository.get_node(str(edge.get("source"))) or {}
            percent = float(edge.get("percent") or 0.0) * 100
            holders.append(f"{holder.get('name', edge.get('source'))}：{percent:.2f}%")
        holders.sort(key=lambda item: float(item.rsplit("：", 1)[1].rstrip("%")), reverse=True)
        return ReportSection(
            title="股权结构",
            content="直接股东及持股比例：" if holders else "暂无股权记录。",
            bullets=holders,
            data={"holderCount": len(holders)},
        )

    def _risk_section(self, score: RiskScore) -> ReportSection:
        bullets = [
            f"{reading.indicator_name}：{reading.sub_score:.0f} 分"
            f"（权重 {reading.weight:.2f}，贡献 {reading.contribution:.1f}）"
            for reading in score.readings
        ]
        return ReportSection(
            title="风险画像",
            content=score.explain(),
            bullets=bullets,
            data={
                "score": score.score,
                "level": score.level.value,
                "modelVersion": score.model_version,
            },
        )

    def _transmission_section(self, assessment: RiskAssessment) -> ReportSection:
        paths = assessment.transmission_paths
        bullets = [
            f"{path.to_text()}（传导分 {path.propagated_score:.1f}，瓶颈 {path.bottleneck:.2f}）"
            for path in paths[:8]
        ]
        return ReportSection(
            title="风险传导",
            content=(
                f"识别到 {len(paths)} 条传导路径，"
                f"覆盖 {len(assessment.affected_entities)} 个受影响主体。"
                if paths
                else "未识别到风险传导路径。"
            ),
            bullets=bullets,
            data={
                "pathCount": len(paths),
                "affectedCount": len(assessment.affected_entities),
            },
        )

    def _event_section(self, entity_id: str) -> ReportSection:
        events: list[str] = []
        for edge in self.repository.neighbours(entity_id):
            if edge.get("relationType") != "INVOLVED_IN":
                continue
            if edge.get("target") != entity_id:
                continue
            event = self.repository.get_node(str(edge.get("source"))) or {}
            events.append(
                f"{event.get('occurredDate', '日期未知')}｜"
                f"{event.get('eventType', '未知类型')}｜"
                f"{event.get('severity', '未知')}｜"
                f"{event.get('description') or '无描述'}"
            )
        events.sort(reverse=True)
        return ReportSection(
            title="风险事件",
            content=f"共 {len(events)} 条事件记录。" if events else "暂无事件记录。",
            bullets=events[:15],
            data={"eventCount": len(events)},
        )

    def _comparables_section(self, entity_id: str) -> ReportSection:
        try:
            cases = self.similar_cases.find(entity_id, top_k=5)
        except Exception as exc:  # comparables are optional
            logger.warning("similar case lookup failed for %s: %s", entity_id, exc)
            cases = []
        bullets = [
            f"{case.entity_name}：相似度 {case.similarity:.2f}，"
            f"风险分 {case.risk_score:.1f}（{case.risk_level}）｜{case.reason}"
            for case in cases
        ]
        return ReportSection(
            title="可比案例",
            content="结构相近的主体：" if bullets else "未找到结构可比主体。",
            bullets=bullets,
            data={"caseCount": len(cases)},
        )

    def _takeaway_section(self, score: RiskScore, assessment: RiskAssessment) -> ReportSection:
        """The analyst-facing conclusion, derived from the data."""
        points: list[str] = []
        if score.level.value in {"high", "critical"}:
            points.append(f"主体风险等级为 {score.level.value}，需在授信与投后环节重点核查。")
        else:
            points.append(f"主体风险等级为 {score.level.value}，暂未触发高等级预警。")

        contributors = score.top_contributors
        if contributors:
            points.append(
                f"风险主要来自{contributors[0].indicator_name}"
                f"（贡献 {contributors[0].contribution:.1f} 分）。"
            )
        if assessment.alerts:
            critical = [
                alert
                for alert in assessment.alerts
                if alert.severity.value in {"major", "critical"}
            ]
            if critical:
                points.append(
                    "触发 " + "、".join(alert.rule_name for alert in critical[:3]) + " 等规则。"
                )
        if assessment.transmission_paths:
            deepest = max(assessment.transmission_paths, key=lambda path: path.hops)
            points.append(
                f"风险可沿 {deepest.hops} 跳传导，最远涉及 {deepest.node_names[-1]}，"
                f"路径瓶颈传导系数 {deepest.bottleneck:.2f}。"
            )
        return ReportSection(
            title="关注要点",
            content="基于图谱事实自动生成的核查要点，供人工复核。",
            bullets=points,
            data={"alertCount": len(assessment.alerts)},
        )


__all__ = ["ReportGenerator", "ReportSection", "ResearchReport"]

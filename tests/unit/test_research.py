"""Unit tests for the research module."""

from __future__ import annotations

import pytest

from fin_kg.core.errors import NotFoundError, ValidationError
from fin_kg.research.kgqa import KGQA
from fin_kg.research.planner import (
    QueryIntent,
    QueryPlanner,
    classify_intent,
    extract_entity_name,
)
from fin_kg.research.report import ReportGenerator
from fin_kg.research.similarity import SimilarCaseFinder, cosine_similarity


class TestQueryPlanner:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("某公司的担保链有多长？", QueryIntent.GUARANTEE_CHAIN),
            ("某公司的股权结构是什么？", QueryIntent.HOLDING_STRUCTURE),
            ("某公司的风险分是多少？", QueryIntent.RISK_ASSESSMENT),
            ("某公司有哪些事件？", QueryIntent.EVENT_HISTORY),
            ("某公司的关联企业有哪些？", QueryIntent.RELATED_PARTIES),
            ("风险最高的前 10 家公司", QueryIntent.TOP_RISKED),
            ("某公司的风险敞口有多大？", QueryIntent.EXPOSURE),
        ],
    )
    def test_intent_classification(self, question: str, expected: QueryIntent) -> None:
        assert classify_intent(question)[0] is expected

    def test_unknown_question(self) -> None:
        assert classify_intent("今天天气怎么样")[0] is QueryIntent.UNKNOWN

    def test_extract_entity_name(self) -> None:
        assert extract_entity_name("北京示例科技有限公司的股权结构") == "北京示例科技有限公司"

    def test_plan_extracts_hops(self) -> None:
        plan = QueryPlanner().plan("某公司的 3 跳担保链")
        assert plan.hops == 3

    def test_plan_extracts_limit(self) -> None:
        plan = QueryPlanner().plan("风险最高的前 25 家公司")
        assert plan.limit == 25

    def test_empty_question_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            QueryPlanner().plan("   ")

    def test_plan_is_serialisable(self) -> None:
        plan = QueryPlanner().plan("某公司的风险分")
        assert plan.as_dict()["intent"] == "risk_assessment"


class TestKGQA:
    def test_answer_carries_evidence(self, seeded_repository) -> None:
        qa = KGQA(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        if not companies:
            pytest.skip("no companies")
        answer = qa.ask(f"{companies[0]['name']}的股权结构是什么？")
        assert answer.intent is QueryIntent.HOLDING_STRUCTURE
        assert answer.evidence

    def test_top_risked_question(self, seeded_repository) -> None:
        qa = KGQA(seeded_repository)
        answer = qa.ask("风险最高的前 5 家公司")
        assert answer.intent is QueryIntent.TOP_RISKED
        assert answer.data["count"] >= 0

    def test_unknown_entity_raises(self, seeded_repository) -> None:
        qa = KGQA(seeded_repository)
        with pytest.raises(NotFoundError):
            qa.ask("不存在于此图谱中的公司风险分是多少？")

    def test_answer_is_serialisable(self, seeded_repository) -> None:
        qa = KGQA(seeded_repository)
        payload = qa.ask("风险最高的前 3 家公司").as_dict()
        assert "answer" in payload
        assert "evidence" in payload

    def test_guarantee_chain_question(self, seeded_repository) -> None:
        qa = KGQA(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        if not companies:
            pytest.skip("no companies")
        answer = qa.ask(f"{companies[0]['name']}的担保链")
        assert answer.intent is QueryIntent.GUARANTEE_CHAIN
        assert answer.data["chainCount"] >= 0


class TestSimilarCases:
    def test_cosine_similarity_of_identical_vectors(self) -> None:
        import numpy as np

        vector = np.array([1.0, 2.0, 3.0])
        assert cosine_similarity(vector, vector) == pytest.approx(1.0)

    def test_cosine_similarity_of_zero_vector_is_zero(self) -> None:
        import numpy as np

        assert cosine_similarity(np.zeros(3), np.array([1.0, 2.0, 3.0])) == 0.0

    def test_finder_returns_comparables(self, seeded_repository) -> None:
        finder = SimilarCaseFinder(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        if len(companies) < 2:
            pytest.skip("not enough companies")
        cases = finder.find(companies[0]["id"], top_k=5, min_similarity=0.0)
        assert cases
        assert all(case.entity_id != companies[0]["id"] for case in cases)

    def test_finder_excludes_the_entity_itself(self, seeded_repository) -> None:
        finder = SimilarCaseFinder(seeded_repository)
        companies = seeded_repository.find_nodes("Company")
        if not companies:
            pytest.skip("no companies")
        cases = finder.find(companies[0]["id"], top_k=20, min_similarity=0.0)
        assert companies[0]["id"] not in {case.entity_id for case in cases}


class TestReportGenerator:
    def test_report_has_all_sections(self, risk_service, seeded_repository) -> None:
        scores = risk_service.rank(limit=1)
        if not scores:
            pytest.skip("no scored entities")
        report = ReportGenerator(seeded_repository).generate(
            risk_service.assess(scores[0].entity_id)
        )
        titles = [section.title for section in report.sections]
        assert "主体概况" in titles
        assert "风险画像" in titles
        assert "风险传导" in titles
        assert "关注要点" in titles

    def test_report_renders_markdown(self, risk_service, seeded_repository) -> None:
        scores = risk_service.rank(limit=1)
        if not scores:
            pytest.skip("no scored entities")
        report = ReportGenerator(seeded_repository).generate(
            risk_service.assess(scores[0].entity_id)
        )
        markdown = report.to_markdown()
        assert markdown.startswith("#")
        assert "风险画像" in markdown

    def test_report_is_serialisable(self, risk_service, seeded_repository) -> None:
        scores = risk_service.rank(limit=1)
        if not scores:
            pytest.skip("no scored entities")
        payload = (
            ReportGenerator(seeded_repository)
            .generate(risk_service.assess(scores[0].entity_id))
            .as_dict()
        )
        assert payload["sections"]

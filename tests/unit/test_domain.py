"""Unit tests for the domain model."""

from __future__ import annotations

import pytest

from fin_kg.domain.entities import (
    Company,
    Event,
    EventType,
    GraphNode,
    Industry,
    Person,
    PersonRole,
    Product,
    RiskLevel,
    Severity,
    node_to_properties,
)
from fin_kg.domain.indicators import (
    RISK_INDICATORS,
    IndicatorCategory,
    IndicatorDirection,
    indicators_by_category,
    validate_indicator_weights,
)
from fin_kg.domain.provenance import ExtractionMethod, Provenance, SourceType
from fin_kg.domain.relations import RELATION_SCHEMA, Relation, RelationType
from fin_kg.domain.risk import IndicatorReading, RiskScore


class TestProvenance:
    def test_fingerprint_is_stable(self) -> None:
        provenance = Provenance(
            source_type=SourceType.REGISTRY,
            source_id="registry:basic",
            record_id="C001",
        )
        assert provenance.fingerprint() == provenance.fingerprint()

    def test_fingerprint_differs_per_record(self) -> None:
        first = Provenance(source_id="s", record_id="a")
        second = Provenance(source_id="s", record_id="b")
        assert first.fingerprint() != second.fingerprint()

    def test_rejects_non_http_url(self) -> None:
        with pytest.raises(ValueError):
            Provenance(source_id="s", record_id="r", source_url="ftp://example.com")

    def test_synthetic_helper_sets_confidence(self) -> None:
        provenance = Provenance.synthetic()
        assert provenance.source_type is SourceType.SYNTHETIC
        assert provenance.extraction_method is ExtractionMethod.SYNTHETIC
        assert provenance.confidence == 0.5

    def test_properties_are_primitive(self) -> None:
        properties = Provenance(source_id="s", record_id="r").to_properties()
        assert isinstance(properties["retrievedAt"], str)
        assert properties["confidence"] == 1.0


class TestEntities:
    def test_company_rejects_short_credit_code(self) -> None:
        with pytest.raises(ValueError):
            Company(id="c1", name="测试公司", credit_code="123")

    def test_company_normalises_credit_code(self) -> None:
        company = Company(id="c1", name="测试", credit_code=" 91110000ma01abcdef ")
        assert company.credit_code == "91110000MA01ABCDEF"

    def test_company_label_set(self) -> None:
        company = Company(id="c1", name="测试")
        assert company.label_set() == {"Company", "Entity"}

    def test_event_defaults(self) -> None:
        event = Event(id="e1", name="被执行")
        assert event.event_type is EventType.OTHER
        assert event.severity is Severity.MEDIUM

    def test_person_role_default(self) -> None:
        person = Person(id="p1", name="张三")
        assert person.role is PersonRole.SHAREHOLDER

    def test_product_defaults(self) -> None:
        product = Product(id="pr1", name="某债券")
        assert product.risk_score if hasattr(product, "risk_score") else True

    def test_node_to_properties_drops_none(self) -> None:
        company = Company(id="c1", name="测试", reg_capital=None)
        properties = node_to_properties(company)
        assert "reg_capital" not in properties
        assert properties["riskLevel"] == RiskLevel.LOW.value
        assert properties["riskScore"] == 0.0

    def test_node_to_properties_serialises_enums(self) -> None:
        company = Company(id="c1", name="测试", industry=Industry.FINANCE)
        properties = node_to_properties(company)
        assert properties["industry"] == "finance"

    def test_base_node_rejects_unknown_field(self) -> None:
        with pytest.raises(ValueError):
            GraphNode(id="n1", name="x", unknown_field=1)  # type: ignore[call-arg]


class TestRiskLevel:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0, RiskLevel.LOW),
            (40, RiskLevel.MEDIUM),
            (70, RiskLevel.HIGH),
            (90, RiskLevel.CRITICAL),
        ],
    )
    def test_from_score(self, score: float, expected: RiskLevel) -> None:
        assert RiskLevel.from_score(score) is expected

    def test_rank_is_ordered(self) -> None:
        assert (
            RiskLevel.LOW.rank
            < RiskLevel.MEDIUM.rank
            < RiskLevel.HIGH.rank
            < RiskLevel.CRITICAL.rank
        )


class TestIndicators:
    def test_weights_sum_to_one(self) -> None:
        assert validate_indicator_weights()

    def test_indicator_ids_are_unique(self) -> None:
        ids = [spec.id for spec in RISK_INDICATORS]
        assert len(ids) == len(set(ids))

    def test_every_indicator_has_a_measurement(self) -> None:
        for spec in RISK_INDICATORS:
            assert spec.measurement_cypher.strip()

    def test_thresholds_are_ascending(self) -> None:
        for spec in RISK_INDICATORS:
            values = [threshold for threshold, _ in spec.thresholds]
            assert values == sorted(values), spec.id

    def test_score_below_first_threshold_is_zero(self) -> None:
        spec = RISK_INDICATORS[0]
        assert spec.score(-1) == 0.0

    def test_score_above_last_threshold_is_hundred(self) -> None:
        spec = RISK_INDICATORS[0]
        assert spec.score(10_000) == 100.0

    def test_missing_measurement_scores_zero(self) -> None:
        assert RISK_INDICATORS[0].score(None) == 0.0

    def test_lower_is_risker_inverts_the_measurement(self) -> None:
        spec = next(
            item
            for item in RISK_INDICATORS
            if item.direction is IndicatorDirection.LOWER_IS_RISKIER
        )
        # A measurement below every threshold is the riskiest case.
        below_all = spec.score(spec.thresholds[0][0] - 0.01)
        # A measurement above every threshold carries no risk.
        above_all = spec.score(spec.thresholds[-1][0] + 0.01)
        assert below_all > above_all

    def test_categories_are_covered(self) -> None:
        for category in IndicatorCategory:
            assert indicators_by_category(category)


class TestRelations:
    def test_hold_requires_percent(self) -> None:
        with pytest.raises(ValueError):
            Relation(
                source_id="a",
                target_id="b",
                relation_type=RelationType.HOLD,
                properties={},
            )

    def test_guarantee_requires_amount(self) -> None:
        with pytest.raises(ValueError):
            Relation(
                source_id="a",
                target_id="b",
                relation_type=RelationType.GUARANTEE,
                properties={},
            )

    def test_self_loop_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Relation(
                source_id="a",
                target_id="a",
                relation_type=RelationType.RELATED,
                properties={},
            )

    def test_transmissibility_comes_from_schema(self) -> None:
        relation = Relation(
            source_id="a",
            target_id="b",
            relation_type=RelationType.GUARANTEE,
            properties={"guaranteed_amount": 100},
        )
        assert relation.transmissibility == RELATION_SCHEMA[RelationType.GUARANTEE][
            "transmissibility"
        ]

    def test_transmissibility_can_be_overridden(self) -> None:
        relation = Relation(
            source_id="a",
            target_id="b",
            relation_type=RelationType.RELATED,
            properties={"transmissibility": 0.1},
        )
        assert relation.transmissibility == 0.1

    def test_properties_include_type(self) -> None:
        relation = Relation(
            source_id="a",
            target_id="b",
            relation_type=RelationType.HOLD,
            properties={"percent": 0.5},
        )
        assert relation.to_properties()["relationType"] == "HOLD"
        assert relation.to_properties()["percent"] == 0.5


class TestRiskScore:
    def test_explain_lists_the_top_contributors(self) -> None:
        readings = [
            IndicatorReading(
                indicator_id="A",
                indicator_name="指标A",
                version="1.0",
                category="legal",
                measurement=10,
                sub_score=80.0,
                weight=0.5,
            ),
            IndicatorReading(
                indicator_id="B",
                indicator_name="指标B",
                version="1.0",
                category="legal",
                measurement=1,
                sub_score=20.0,
                weight=0.5,
            ),
        ]
        score = RiskScore(entity_id="c1", entity_name="测试", score=50, readings=readings)
        assert "指标A" in score.explain()
        assert score.top_contributors[0].indicator_id == "A"

    def test_explain_without_readings(self) -> None:
        score = RiskScore(entity_id="c1", entity_name="测试", score=0)
        assert "无可用指标" in score.explain()

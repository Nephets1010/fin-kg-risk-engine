"""Regression tests for the defects found in the QA audit round.

Each test guards a contract that was previously broken. They are grouped by
the defect they protect rather than by module, because the point of a
regression test is to name the behaviour it keeps.

- ``parse_amount``: thousands separators used to truncate the number.
- ``_merge_provenance``: the merge condition was inverted, so the stored
  provenance was always dropped.
- ``IND-HLD-001``: the Cypher and the local engine disagreed on what
  "no shareholder" means — 60 points apart.
- ``occurredDate``: stored as a string, compared against a ``date()``.
- ``_persist_relations``: one malformed relation aborted the whole run.
- ``alert_fingerprint``: built from the message, so it changed every run.
- ``propagate``: reported an exposed entity once per path.
"""

from __future__ import annotations

from datetime import date

import pytest

from fin_kg.core.errors import GraphStoreError
from fin_kg.domain.entities import Company, Event
from fin_kg.domain.indicators import INDICATOR_BY_ID
from fin_kg.domain.provenance import ExtractionMethod, Provenance, SourceType
from fin_kg.domain.relations import Relation, RelationType
from fin_kg.graph.repository import InMemoryGraphRepository
from fin_kg.ingestion.cleaning import parse_amount
from fin_kg.ingestion.pipeline import IngestionPipeline
from fin_kg.risk.engine import RuleEngine, alert_fingerprint
from fin_kg.risk.propagation import RiskPropagator
from fin_kg.risk.service import RiskService


class TestThousandsSeparators:
    """``5,000万元`` used to parse as ``5``."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("5,000万元", 5000.0),
            ("12,345,678元", 1234.5678),
            ("1,000,000,000元", 100000.0),
            ("1,234.56万", 1234.56),
            ("1.5亿", 15000.0),
            ("5000", 5000.0),
            ("100万", 100.0),
        ],
    )
    def test_the_separator_is_stripped(self, value: str, expected: float) -> None:
        assert parse_amount(value) == pytest.approx(expected)

    def test_a_two_digit_group_is_not_a_separator(self) -> None:
        """``2024,10`` is not a thousands-separated number."""
        assert parse_amount("2024,10") == pytest.approx(2024.0)


class TestProvenanceMerge:
    """The merge condition was inverted: the stored side was dropped."""

    @staticmethod
    def _provenance(source_id: str, record_id: str) -> Provenance:
        return Provenance(
            source_id=source_id,
            source_type=SourceType.MANUAL,
            extraction_method=ExtractionMethod.MANUAL_ENTRY,
            confidence=0.9,
            record_id=record_id,
        )

    def test_the_stored_provenance_is_kept(self) -> None:
        incoming = self._provenance("srcA", "rec-1")
        stored = self._provenance("srcB", "rec-2")
        node = Company(id="c1", name="甲", provenance=[incoming])

        IngestionPipeline._merge_provenance(node, {"provenance": [stored.to_properties()]})

        assert len(node.provenance) == 2, "已存储的溯源被丢弃"
        assert {item.source_id for item in node.provenance} == {"srcA", "srcB"}

    def test_the_same_fact_is_not_duplicated(self) -> None:
        provenance = self._provenance("srcA", "rec-1")
        node = Company(id="c1", name="甲", provenance=[provenance])

        IngestionPipeline._merge_provenance(node, {"provenance": [provenance.to_properties()]})

        assert len(node.provenance) == 1

    def test_a_json_encoded_store_is_merged(self) -> None:
        import json

        incoming = self._provenance("srcA", "rec-1")
        stored = self._provenance("srcB", "rec-2")
        node = Company(id="c1", name="甲", provenance=[incoming])

        IngestionPipeline._merge_provenance(
            node, {"provenance": json.dumps([stored.to_properties()])}
        )

        assert len(node.provenance) == 2


class TestShareholdingSemantics:
    """The Cypher and the local engine disagreed by 60 points."""

    def test_the_cypher_does_not_coalesce_to_zero(self) -> None:
        spec = INDICATOR_BY_ID["IND-HLD-001"]
        assert "coalesce" not in spec.measurement_cypher.lower()

    def test_a_missing_shareholder_is_not_extreme_dispersion(self) -> None:
        """A zero stake means "extremely dispersed" for this indicator.

        Scoring a company whose shareholder data is missing at the maximum
        risk level would be a false positive, so both engines must report
        "no data" instead.
        """
        spec = INDICATOR_BY_ID["IND-HLD-001"]
        assert spec.score(None) == 0.0
        assert spec.score(0.0) == 60.0, "阈值表本身不应改动"

    def test_the_local_engine_returns_none_without_shareholders(self) -> None:
        from fin_kg.risk.indicators_engine import LocalIndicatorEngine

        repository = InMemoryGraphRepository()
        repository.ensure_schema()
        repository.upsert_nodes([Company(id="c1", name="甲")])

        value = LocalIndicatorEngine(repository).measure(
            INDICATOR_BY_ID["IND-HLD-001"], "c1", since=date(2026, 1, 1)
        )
        assert value is None


class TestEventDateComparison:
    """``occurredDate`` is a string; it must be converted before comparison."""

    @pytest.mark.parametrize(
        "indicator_id",
        ["IND-LIT-001", "IND-ENF-001", "IND-PEN-001", "IND-RTE-001", "IND-NEG-001"],
    )
    def test_the_date_is_converted(self, indicator_id: str) -> None:
        spec = INDICATOR_BY_ID[indicator_id]
        assert "date(e.occurredDate)" in spec.measurement_cypher, (
            f"{indicator_id} 未转换日期类型，在 Neo4j 上会抛类型错误"
        )

    def test_the_stored_value_is_a_string(self) -> None:
        from fin_kg.domain.entities import node_to_properties

        event = Event(id="e1", name="涉诉", event_type="litigation", occurred_date=date(2026, 3, 1))
        assert isinstance(node_to_properties(event)["occurredDate"], str)


class TestRelationIsolation:
    """One malformed relation used to abort the whole ingestion."""

    def _relation(self, target: str) -> Relation:
        return Relation(
            source_id="c1",
            target_id=target,
            relation_type=RelationType.HOLD,
            properties={"percent": 0.5},
        )

    def test_a_bad_relation_does_not_stop_the_batch(self) -> None:
        repository = InMemoryGraphRepository()
        repository.ensure_schema()
        repository.upsert_nodes([Company(id="c1", name="甲"), Company(id="c2", name="乙")])

        written = IngestionPipeline(repository)._persist_relations(
            [self._relation("c2"), self._relation("MISSING")]
        )

        assert written == 1, "坏关系导致整批失败"
        assert len(repository.snapshot.edges) == 1

    def test_a_storage_failure_is_not_swallowed(self) -> None:
        """A broken store must surface, not be skipped one record at a time."""

        class BrokenStore(InMemoryGraphRepository):
            def upsert_relations(self, relations):
                raise GraphStoreError(
                    "graph store query failed", details={"error": "connection refused"}
                )

        repository = BrokenStore()
        repository.ensure_schema()
        repository.upsert_nodes([Company(id="c1", name="甲"), Company(id="c2", name="乙")])

        with pytest.raises(GraphStoreError):
            IngestionPipeline(repository)._persist_relations([self._relation("c2")])


class TestAlertFingerprint:
    """The fingerprint used to include the rendered message."""

    @staticmethod
    def _alert(message: str):
        from fin_kg.domain.risk import AlertSeverity, RiskAlert

        return RiskAlert(
            alert_id="a1",
            rule_id="R-01",
            rule_name="测试规则",
            entity_id="c1",
            entity_name="甲",
            severity=AlertSeverity.MAJOR,
            score=70.0,
            message=message,
        )

    def test_the_fingerprint_survives_a_message_change(self) -> None:
        first = alert_fingerprint(self._alert("风险分 70.0，命中 1 条路径"))
        second = alert_fingerprint(self._alert("风险分 75.5，命中 2 条路径"))
        assert first == second, "指纹随消息变化，跨轮次去重失效"

    def test_a_different_rule_gets_a_different_fingerprint(self) -> None:
        alert = self._alert("x")
        other = self._alert("x")
        other.rule_id = "R-02"
        assert alert_fingerprint(alert) != alert_fingerprint(other)


class TestAffectedEntitiesAreUnique:
    """An exposed entity was reported once per path."""

    def test_an_entity_reachable_by_two_chains_appears_once(self) -> None:
        repository = InMemoryGraphRepository()
        repository.ensure_schema()
        repository.upsert_nodes(
            [
                Company(id=node_id, name=name)
                for node_id, name in {"A": "甲", "B": "乙", "C": "丙", "D": "丁"}.items()
            ]
        )
        repository.upsert_relations(
            [
                Relation(
                    source_id=source,
                    target_id=target,
                    relation_type=RelationType.GUARANTEE,
                    properties={"guaranteed_amount": 100},
                )
                for source, target in [("A", "B"), ("B", "D"), ("A", "C"), ("C", "D")]
            ]
        )
        service = RiskService(
            repository=repository,
            rules=RuleEngine(rules=[]),
            indicator_engine=__import__(
                "fin_kg.risk.indicators_engine", fromlist=["LocalIndicatorEngine"]
            ).LocalIndicatorEngine(repository),
        )
        score = service._score_known("A", repository.get_node("A"))
        score.score = 80.0

        result = RiskPropagator(repository, max_hops=3, decay=0.6).propagate(score)

        identifiers = [item["id"] for item in result.affected]
        assert len(identifiers) == len(set(identifiers)), "同一受影响实体重复出现"
        assert set(identifiers) == {"B", "C", "D"}

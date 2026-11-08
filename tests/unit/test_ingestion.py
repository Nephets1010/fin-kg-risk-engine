"""Unit tests for the ingestion layer."""

from __future__ import annotations

from datetime import date

import pytest

from fin_kg.core.errors import ValidationError
from fin_kg.domain.relations import RelationType
from fin_kg.graph.alignment import EntityResolver, deterministic_id, normalize_name
from fin_kg.ingestion.base import RawRecord, RecordKind
from fin_kg.ingestion.cleaning import (
    Cleaner,
    classify_event_type,
    classify_industry,
    classify_person_role,
    classify_product_type,
    classify_severity,
    classify_status,
    dedupe_key,
    parse_amount,
    parse_date,
    parse_percent,
)
from fin_kg.ingestion.pipeline import IngestionPipeline
from fin_kg.ingestion.sources import CsvSourceAdapter, SyntheticSourceAdapter


class TestParsers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("60%", 0.6), ("60", 0.6), (0.6, 0.6), (60, 0.6), (None, None), ("", None)],
    )
    def test_parse_percent(self, value: object, expected: float | None) -> None:
        assert parse_percent(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # The canonical unit is 万元: a bare number is already in it.
            ("500", 500.0),
            (500, 500.0),
            ("100万", 100.0),
            ("1.5亿", 15_000.0),
            ("2000元", 0.2),
            (None, None),
            ("", None),
        ],
    )
    def test_parse_amount(self, value: object, expected: float | None) -> None:
        assert parse_amount(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2024-03-15", date(2024, 3, 15)),
            ("2024年3月15日", date(2024, 3, 15)),
            ("2024/03/15", date(2024, 3, 15)),
            ("20240315", date(2024, 3, 15)),
            ("not a date", None),
            (None, None),
        ],
    )
    def test_parse_date(self, value: object, expected: date | None) -> None:
        assert parse_date(value) == expected

    def test_classify_industry(self) -> None:
        assert classify_industry("制造业") is not None
        assert classify_industry("银行") is not None
        assert classify_industry("") is not None

    def test_classify_status(self) -> None:
        assert classify_status("存续").value == "active"
        assert classify_status("注销").value == "cancelled"
        assert classify_status("").value == "unknown"

    def test_classify_event_type(self) -> None:
        assert classify_event_type("强制执行").value == "enforcement"
        assert classify_event_type("行政处罚").value == "penalty"
        assert classify_event_type("随便什么").value == "other"

    def test_classify_severity(self) -> None:
        assert classify_severity("重大").value == "critical"
        assert classify_severity("轻微").value == "low"

    def test_classify_person_role(self) -> None:
        assert classify_person_role("法定代表人").value == "legal_rep"
        assert classify_person_role("实际控制人").value == "actual_controller"

    def test_classify_product_type(self) -> None:
        assert classify_product_type("公司债").value == "bond"
        assert classify_product_type("私募基金").value == "fund"


class TestEntityResolver:
    def test_normalize_name_strips_suffix(self) -> None:
        assert normalize_name("北京示例科技有限公司") == "北京示例科技"

    def test_normalize_name_folds_full_width(self) -> None:
        assert normalize_name("ＡＢＣ有限公司") == "ABC"

    def test_deterministic_id_is_stable(self) -> None:
        assert deterministic_id("company", "a", "b") == deterministic_id("company", "a", "b")

    def test_deterministic_id_differs_on_input(self) -> None:
        assert deterministic_id("company", "a") != deterministic_id("company", "b")

    def test_credit_code_wins(self) -> None:
        resolver = EntityResolver()
        company, resolution = resolver.make_company(name="甲公司", credit_code="91110000MA01ABCDEF")
        assert resolution.matched_on == "new"
        assert company.credit_code == "91110000MA01ABCDEF"

        _, second = resolver.make_company(
            name="甲公司（另一处登记）", credit_code="91110000MA01ABCDEF", source_key="k2"
        )
        assert second.matched_on == "credit_code"

    def test_second_record_with_same_credit_code_merges(self) -> None:
        resolver = EntityResolver()
        first, _ = resolver.make_company(
            name="甲公司", credit_code="91110000MA01ABCDEF", source_key="k1"
        )
        second, resolution = resolver.make_company(
            name="甲公司另一名称", credit_code="91110000MA01ABCDEF", source_key="k2"
        )
        assert second.id == first.id
        assert resolution.is_new is False

    def test_same_name_and_region_merges(self) -> None:
        resolver = EntityResolver()
        first, _ = resolver.make_company(
            name="北京示例科技有限公司", region="北京", source_key="k1"
        )
        second, resolution = resolver.make_company(
            name="北京示例科技有限公司", region="北京", source_key="k2"
        )
        assert second.id == first.id
        assert resolution.matched_on == "name_region"

    def test_different_region_creates_new_entity(self) -> None:
        resolver = EntityResolver()
        first, _ = resolver.make_company(
            name="北京示例科技有限公司", region="北京", source_key="k1"
        )
        second, resolution = resolver.make_company(
            name="北京示例科技有限公司", region="上海", source_key="k2"
        )
        assert second.id != first.id
        assert resolution.is_new is True

    def test_alias_takes_precedence(self) -> None:
        resolver = EntityResolver(alias_map={"示例公司": "company-fixed"})
        resolution = resolver.resolve_company(name="示例公司", source_key="k")
        assert resolution.entity_id == "company-fixed"
        assert resolution.matched_on == "alias"

    def test_ambiguous_name_is_flagged(self) -> None:
        resolver = EntityResolver()
        resolver.make_company(name="同名公司", region="北京", source_key="k1")
        resolver.make_company(name="同名公司", region="上海", source_key="k2")
        resolution = resolver.resolve_company(name="同名公司", source_key="k3")
        assert resolution.matched_on == "ambiguous_name"
        assert resolution.confidence < 0.5


class TestCleaner:
    def test_company_with_missing_name_is_rejected(self) -> None:
        cleaner = Cleaner()
        record = RawRecord(
            kind=RecordKind.COMPANY,
            source_id="s",
            record_id="r",
            payload={},
        )
        with pytest.raises(ValidationError):
            cleaner.clean_company(record)

    def test_cleaning_coerces_types(self) -> None:
        cleaner = Cleaner()
        record = RawRecord(
            kind=RecordKind.COMPANY,
            source_id="s",
            record_id="r",
            payload={
                "name": "示例",
                "reg_capital": "100",
                "industry": "制造业",
                "status": "存续",
                "founded_date": "2020年1月1日",
            },
        )
        company = cleaner.clean_company(record)
        assert company.reg_capital == 100.0
        assert company.founded_date == date(2020, 1, 1)

    def test_event_severity_from_description(self) -> None:
        cleaner = Cleaner()
        record = RawRecord(
            kind=RecordKind.EVENT,
            source_id="s",
            record_id="r",
            payload={"name": "重大违规", "description": "涉及重大违规操作"},
        )
        event = cleaner.clean_event(record)
        assert event.severity.value in {"critical", "high"}

    def test_provenance_is_attached(self) -> None:
        cleaner = Cleaner()
        record = RawRecord(
            kind=RecordKind.COMPANY,
            source_id="csv:companies",
            record_id="c1",
            payload={"name": "示例"},
        )
        company = cleaner.clean_company(record)
        assert company.provenance[0].source_id == "csv:companies"

    def test_stats_track_rejections(self) -> None:
        cleaner = Cleaner()
        with pytest.raises(ValidationError):
            cleaner.clean(
                RawRecord(kind=RecordKind.COMPANY, source_id="s", record_id="r", payload={})
            )
        assert cleaner.stats.rejected == 1


class TestDedupeKey:
    def test_relations_with_different_endpoints_differ(self) -> None:
        first = RawRecord(
            kind=RecordKind.RELATION,
            source_id="s",
            record_id="r1",
            payload={"type": "HOLD", "source": "a", "target": "b"},
        )
        second = RawRecord(
            kind=RecordKind.RELATION,
            source_id="s",
            record_id="r2",
            payload={"type": "HOLD", "source": "a", "target": "c"},
        )
        assert dedupe_key(first) != dedupe_key(second)


class TestSources:
    def test_synthetic_source_is_deterministic(self) -> None:
        first = SyntheticSourceAdapter(companies=5, seed=42).fetch()
        second = SyntheticSourceAdapter(companies=5, seed=42).fetch()
        assert len(first.records) == len(second.records)
        assert first.records[0].payload == second.records[0].payload

    def test_synthetic_source_marks_provenance(self) -> None:
        result = SyntheticSourceAdapter(companies=3).fetch()
        assert all(record.source_type.value == "synthetic" for record in result.records)

    def test_synthetic_source_produces_relations(self) -> None:
        result = SyntheticSourceAdapter(companies=10).fetch()
        relations = [record for record in result.records if record.kind is RecordKind.RELATION]
        assert relations

    def test_csv_source_reads_sample_files(self, tmp_path) -> None:
        path = tmp_path / "companies.csv"
        path.write_text("id,name,industry\nC1,示例公司,制造业\n", encoding="utf-8")
        adapter = CsvSourceAdapter(tmp_path)
        result = adapter.fetch()
        assert len(result.records) == 1
        assert result.records[0].payload["name"] == "示例公司"

    def test_csv_source_ignores_unknown_files(self, tmp_path) -> None:
        (tmp_path / "random.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        assert CsvSourceAdapter(tmp_path).fetch().records == []


class TestPipeline:
    def test_pipeline_is_idempotent(self, repository) -> None:
        adapter = SyntheticSourceAdapter(companies=10, seed=1)
        pipeline = IngestionPipeline(repository)
        first = pipeline.run([adapter])
        second = pipeline.run([adapter])
        assert first.nodes_written == second.nodes_written
        assert repository.stats()["Entity"] == first.nodes_written

    def test_checkpoint_is_written(self, repository, tmp_path) -> None:
        pipeline = IngestionPipeline(repository, checkpoint_dir=tmp_path)
        pipeline.run([SyntheticSourceAdapter(companies=5)])
        checkpoints = list(tmp_path.glob("*.json"))
        assert checkpoints
        assert "completed" in checkpoints[0].read_text(encoding="utf-8")

    def test_resume_skips_completed_source(self, repository, tmp_path) -> None:
        adapter = SyntheticSourceAdapter(companies=5, seed=3)
        pipeline = IngestionPipeline(repository, checkpoint_dir=tmp_path)
        pipeline.run([adapter])
        report = pipeline.run([adapter], resume=True)
        assert report.fetched == 0

    def test_relations_are_written_after_nodes(self, repository) -> None:
        pipeline = IngestionPipeline(repository)
        report = pipeline.run([SyntheticSourceAdapter(companies=10)])
        assert report.relations_written > 0
        assert repository.stats()["Relationship"] == report.relations_written

    def test_relation_types_are_valid(self, seeded_repository) -> None:
        for edge in seeded_repository.snapshot.edges:
            assert edge["relationType"] in {relation.value for relation in RelationType}

    def test_no_self_loops(self, seeded_repository) -> None:
        for edge in seeded_repository.snapshot.edges:
            assert edge["source"] != edge["target"]

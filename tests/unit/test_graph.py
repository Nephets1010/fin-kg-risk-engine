"""Unit tests for the graph layer."""

from __future__ import annotations

import pytest

from fin_kg.core.errors import GraphStoreError
from fin_kg.domain.entities import Company, Event, Person
from fin_kg.domain.relations import Relation, RelationType
from fin_kg.graph.repository import (
    ALLOWED_LABELS,
    ALLOWED_RELATION_TYPES,
    VALID_NODE_LABELS,
    VALID_ORDER_COLUMNS,
    FileGraphRepository,
    Neo4jGraphRepository,
    bfs_distances,
)


class TestInMemoryRepository:
    def test_schema_application_is_recorded(self, repository) -> None:
        assert repository.applied_schema_version is not None

    def test_upsert_creates_nodes(self, repository) -> None:
        written = repository.upsert_nodes([Company(id="c1", name="甲公司")])
        assert written == 1
        assert repository.get_node("c1") is not None

    def test_upsert_merges_nodes(self, repository) -> None:
        repository.upsert_nodes([Company(id="c1", name="甲公司")])
        repository.upsert_nodes([Company(id="c1", name="甲公司改名")])
        assert repository.stats()["Entity"] == 1

    def test_credit_code_conflict_is_rejected(self, repository) -> None:
        repository.upsert_nodes([Company(id="c1", name="甲", credit_code="91110000MA01ABCDEF")])
        with pytest.raises(GraphStoreError):
            repository.upsert_nodes([Company(id="c2", name="乙", credit_code="91110000MA01ABCDEF")])

    def test_relation_to_unknown_node_is_rejected(self, repository) -> None:
        repository.upsert_nodes([Company(id="c1", name="甲")])
        with pytest.raises(GraphStoreError):
            repository.upsert_relations(
                [
                    Relation(
                        source_id="c1",
                        target_id="missing",
                        relation_type=RelationType.HOLD,
                        properties={"percent": 0.5},
                    )
                ]
            )

    def test_traverse_follows_the_direction(self, repository) -> None:
        repository.upsert_nodes([Company(id="a", name="A"), Company(id="b", name="B")])
        repository.upsert_relations(
            [
                Relation(
                    source_id="a",
                    target_id="b",
                    relation_type=RelationType.HOLD,
                    properties={"percent": 0.5},
                )
            ]
        )
        outgoing = repository.traverse("a", [RelationType.HOLD], max_hops=1, direction="out")
        incoming = repository.traverse("b", [RelationType.HOLD], max_hops=1, direction="in")
        assert len(outgoing) == 1
        assert len(incoming) == 1
        assert repository.traverse("b", [RelationType.HOLD], max_hops=1, direction="out") == []

    def test_traverse_respects_max_hops(self, repository) -> None:
        repository.upsert_nodes([Company(id=f"c{i}", name=f"C{i}") for i in range(4)])
        repository.upsert_relations(
            [
                Relation(
                    source_id="c0",
                    target_id="c1",
                    relation_type=RelationType.GUARANTEE,
                    properties={"guaranteed_amount": 1},
                ),
                Relation(
                    source_id="c1",
                    target_id="c2",
                    relation_type=RelationType.GUARANTEE,
                    properties={"guaranteed_amount": 1},
                ),
                Relation(
                    source_id="c2",
                    target_id="c3",
                    relation_type=RelationType.GUARANTEE,
                    properties={"guaranteed_amount": 1},
                ),
            ]
        )
        paths = repository.traverse("c0", [RelationType.GUARANTEE], max_hops=2, direction="out")
        assert max(path.length for path in paths) == 2

    def test_find_nodes_filters_by_label(self, repository) -> None:
        repository.upsert_nodes([Company(id="c1", name="甲"), Person(id="p1", name="张三")])
        assert len(repository.find_nodes("Company")) == 1
        assert len(repository.find_nodes("Person")) == 1
        assert len(repository.find_nodes("Event")) == 0

    def test_stats_counts_labels(self, repository) -> None:
        repository.upsert_nodes([Company(id="c1", name="甲"), Event(id="e1", name="事件")])
        stats = repository.stats()
        assert stats["Company"] == 1
        assert stats["Event"] == 1
        assert stats["Relationship"] == 0

    def test_bfs_distances(self, repository) -> None:
        repository.upsert_nodes([Company(id=f"c{i}", name=f"C{i}") for i in range(3)])
        repository.upsert_relations(
            [
                Relation(
                    source_id="c0",
                    target_id="c1",
                    relation_type=RelationType.GUARANTEE,
                    properties={"guaranteed_amount": 1},
                ),
                Relation(
                    source_id="c1",
                    target_id="c2",
                    relation_type=RelationType.GUARANTEE,
                    properties={"guaranteed_amount": 1},
                ),
            ]
        )
        distances = bfs_distances(repository, "c0", [RelationType.GUARANTEE], max_hops=3)
        assert distances["c1"] == 1
        assert distances["c2"] == 2


class TestWhitelists:
    def test_labels_are_whitelisted(self) -> None:
        assert "Company" in ALLOWED_LABELS
        assert "Entity" in ALLOWED_LABELS

    def test_relation_types_are_whitelisted(self) -> None:
        assert "HOLD" in ALLOWED_RELATION_TYPES
        assert "GUARANTEE" in ALLOWED_RELATION_TYPES

    def test_whitelist_matches_the_domain(self) -> None:
        assert {relation.value for relation in RelationType} == ALLOWED_RELATION_TYPES


class TestReadPathWhitelist:
    """R7-A01: neither a label nor an ORDER BY key may reach Cypher unchecked."""

    MALICIOUS_LABEL = "Company) DETACH DELETE n MATCH (m:Company"

    def test_valid_labels_alias_the_write_whitelist(self) -> None:
        assert VALID_NODE_LABELS is ALLOWED_LABELS
        assert {"Entity", "Company", "Person", "Event", "Product"} == VALID_NODE_LABELS
        assert {"riskScore", "createdAt", "updatedAt"} == VALID_ORDER_COLUMNS

    @pytest.mark.parametrize("label", sorted(VALID_NODE_LABELS))
    def test_every_declared_label_is_queryable(self, repository, label: str) -> None:
        repository.upsert_nodes([Company(id="c1", name="甲")])
        assert repository.find_nodes(label) is not None

    @pytest.mark.parametrize("order_by", sorted(VALID_ORDER_COLUMNS))
    def test_declared_order_columns_pass_the_guard(self, repository, order_by: str) -> None:
        # Sorting by timestamps is a store-specific capability; an empty
        # graph isolates the guard itself from the in-memory sort key parser.
        assert repository.find_nodes("Company", order_by=order_by) == []

    def test_risk_score_ordering_still_works(self, repository) -> None:
        repository.upsert_nodes(
            [
                Company(id="c1", name="甲", risk_score=10.0),
                Company(id="c2", name="乙", risk_score=90.0),
            ]
        )
        assert [node["id"] for node in repository.find_nodes("Company", order_by="riskScore")] == [
            "c2",
            "c1",
        ]

    def test_in_memory_rejects_an_injection_label(self, repository) -> None:
        with pytest.raises(GraphStoreError, match="unknown label"):
            repository.find_nodes(self.MALICIOUS_LABEL)

    def test_in_memory_rejects_an_injection_order_by(self, repository) -> None:
        with pytest.raises(GraphStoreError, match="unknown property"):
            repository.find_nodes("Company", order_by="riskScore DESC, n.name); DETACH DELETE n")

    def test_neo4j_rejects_before_the_driver_is_called(self) -> None:
        class _ExplodingDriver:
            def session(self, **kwargs):  # pragma: no cover - must never be reached
                raise AssertionError("the driver must not see an unvalidated query")

        repository = Neo4jGraphRepository(_ExplodingDriver())
        with pytest.raises(GraphStoreError, match="unknown label"):
            repository.find_nodes(self.MALICIOUS_LABEL)
        with pytest.raises(GraphStoreError, match="unknown property"):
            repository.find_nodes("Company", order_by="riskScore); DETACH DELETE n")

    def test_execute_read_labels_still_pass_the_guard(self, repository) -> None:
        """Module-owned queries extract their label and go through find_nodes."""
        repository.upsert_nodes([Company(id="c1", name="甲")])
        rows = repository.execute_read("MATCH (n:Company) WHERE true RETURN n")
        assert len(rows) == 1
        with pytest.raises(GraphStoreError, match="unknown label"):
            repository.execute_read("MATCH (n:UndeclaredLabel) RETURN n")


class TestBulkWrite:
    """R7-A03: the file store must collapse N updates into one save."""

    def test_bulk_update_saves_once(self, tmp_path) -> None:
        repository = FileGraphRepository(tmp_path / "graph.json")
        repository.upsert_nodes([Company(id=f"c{i}", name=f"C{i}") for i in range(5)])
        calls: list[int] = []
        original_save = repository.save

        def counting_save() -> None:
            calls.append(1)
            original_save()

        repository.save = counting_save  # type: ignore[method-assign]

        updates = {f"c{i}": {"riskScore": float(i)} for i in range(5)}
        assert repository.update_properties_bulk(updates) == 5
        assert len(calls) == 1
        assert repository.get_node("c4")["riskScore"] == 4.0

    def test_bulk_update_with_autosave_off_never_saves(self, tmp_path) -> None:
        repository = FileGraphRepository(tmp_path / "graph.json", autosave=False)
        repository.upsert_nodes([Company(id="c1", name="甲")])
        calls: list[int] = []

        def counting_save() -> None:
            calls.append(1)

        repository.save = counting_save  # type: ignore[method-assign]
        assert repository.update_properties_bulk({"c1": {"riskScore": 1.0}}) == 1
        assert calls == []
        assert repository.get_node("c1")["riskScore"] == 1.0

    def test_stats_entity_is_the_unique_node_count(self, repository) -> None:
        repository.upsert_nodes(
            [Company(id="c1", name="甲"), Company(id="c2", name="乙"), Event(id="e1", name="事件")]
        )
        stats = repository.stats()
        assert stats["Entity"] == 3
        assert stats["Entity"] == len(repository.snapshot.nodes)
        # Naively summing the per-type labels counts every node twice.
        assert stats["Company"] + stats["Event"] + stats["Entity"] == 6


class TestSchema:
    def test_migrations_are_versioned_sequentially(self) -> None:
        from fin_kg.graph.schema import MIGRATIONS

        versions = [migration.version for migration in MIGRATIONS]
        assert versions == sorted(versions)
        assert versions == list(range(1, len(versions) + 1))

    def test_every_migration_has_statements(self) -> None:
        from fin_kg.graph.schema import MIGRATIONS

        assert all(migration.statements for migration in MIGRATIONS)

    def test_statements_are_idempotent(self) -> None:
        from fin_kg.graph.schema import MIGRATIONS

        for migration in MIGRATIONS:
            for statement in migration.statements:
                assert "IF NOT EXISTS" in statement

    def test_constraints_and_indexes_are_separable(self) -> None:
        from fin_kg.graph.schema import constraint_statements, index_statements

        assert constraint_statements()
        assert index_statements()

    def test_describe_schema_mentions_every_relation(self) -> None:
        from fin_kg.graph.schema import describe_schema

        text = describe_schema()
        for relation in RelationType:
            assert relation.value in text

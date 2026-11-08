"""Unit tests for the graph layer."""

from __future__ import annotations

import pytest

from fin_kg.core.errors import GraphStoreError
from fin_kg.domain.entities import Company, Event, Person
from fin_kg.domain.relations import Relation, RelationType
from fin_kg.graph.repository import (
    ALLOWED_LABELS,
    ALLOWED_RELATION_TYPES,
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
        repository.upsert_nodes(
            [Company(id="c1", name="甲", credit_code="91110000MA01ABCDEF")]
        )
        with pytest.raises(GraphStoreError):
            repository.upsert_nodes(
                [Company(id="c2", name="乙", credit_code="91110000MA01ABCDEF")]
            )

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
        repository.upsert_nodes(
            [Company(id="a", name="A"), Company(id="b", name="B")]
        )
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
        outgoing = repository.traverse(
            "a", [RelationType.HOLD], max_hops=1, direction="out"
        )
        incoming = repository.traverse(
            "b", [RelationType.HOLD], max_hops=1, direction="in"
        )
        assert len(outgoing) == 1
        assert len(incoming) == 1
        assert repository.traverse(
            "b", [RelationType.HOLD], max_hops=1, direction="out"
        ) == []

    def test_traverse_respects_max_hops(self, repository) -> None:
        repository.upsert_nodes(
            [Company(id=f"c{i}", name=f"C{i}") for i in range(4)]
        )
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
        paths = repository.traverse(
            "c0", [RelationType.GUARANTEE], max_hops=2, direction="out"
        )
        assert max(path.length for path in paths) == 2

    def test_find_nodes_filters_by_label(self, repository) -> None:
        repository.upsert_nodes(
            [Company(id="c1", name="甲"), Person(id="p1", name="张三")]
        )
        assert len(repository.find_nodes("Company")) == 1
        assert len(repository.find_nodes("Person")) == 1
        assert len(repository.find_nodes("Event")) == 0

    def test_stats_counts_labels(self, repository) -> None:
        repository.upsert_nodes(
            [Company(id="c1", name="甲"), Event(id="e1", name="事件")]
        )
        stats = repository.stats()
        assert stats["Company"] == 1
        assert stats["Event"] == 1
        assert stats["Relationship"] == 0

    def test_bfs_distances(self, repository) -> None:
        repository.upsert_nodes(
            [Company(id=f"c{i}", name=f"C{i}") for i in range(3)]
        )
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
        distances = bfs_distances(
            repository, "c0", [RelationType.GUARANTEE], max_hops=3
        )
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
        assert ALLOWED_RELATION_TYPES == {relation.value for relation in RelationType}


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

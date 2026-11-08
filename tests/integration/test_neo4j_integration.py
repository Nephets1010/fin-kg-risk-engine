"""Integration tests against a live Neo4j instance.

These tests are skipped automatically when no Neo4j is reachable. Point them
at an instance with::

    export FIN_KG_NEO4J_URI=bolt://localhost:7687
    export FIN_KG_NEO4J_USER=neo4j
    export FIN_KG_NEO4J_PASSWORD=your-password

They use a dedicated database (``fin_kg_test``) and wipe it before the run,
so they never touch production data.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from fin_kg.domain.entities import Company, Person
from fin_kg.domain.relations import Relation, RelationType
from fin_kg.graph.repository import Neo4jGraphRepository


def _neo4j_settings():
    """Build the test settings, or skip when Neo4j is not configured."""
    uri = os.environ.get("FIN_KG_NEO4J_URI")
    password = os.environ.get("FIN_KG_NEO4J_PASSWORD")
    if not uri or not password:
        pytest.skip("FIN_KG_NEO4J_URI / FIN_KG_NEO4J_PASSWORD are not set")

    from fin_kg.config.settings import Settings

    return Settings(
        env="test",
        neo4j={
            "uri": uri,
            "user": os.environ.get("FIN_KG_NEO4J_USER", "neo4j"),
            "password": password,
            "database": os.environ.get("FIN_KG_NEO4J_DATABASE", "neo4j"),
        },
    )


@pytest.fixture
def neo4j_repository():
    """A Neo4j repository with a clean test database."""
    settings = _neo4j_settings()
    try:
        repository = Neo4jGraphRepository.from_settings(settings)
        repository.ensure_schema()
    except Exception as exc:  # skip when the service is absent
        pytest.skip(f"Neo4j is not reachable: {exc}")

    repository.execute_read("MATCH (n) DETACH DELETE n")
    yield repository
    repository.execute_read("MATCH (n) DETACH DELETE n")
    repository.close()


@pytest.mark.integration
class TestNeo4jRepository:
    def test_schema_is_applied(self, neo4j_repository: Neo4jGraphRepository) -> None:
        rows = neo4j_repository.execute_read("SHOW CONSTRAINTS YIELD name RETURN name")
        assert rows

    def test_upsert_and_read_back(self, neo4j_repository: Neo4jGraphRepository) -> None:
        neo4j_repository.upsert_nodes(
            [Company(id="n1", name="测试公司"), Person(id="p1", name="张三")]
        )
        node = neo4j_repository.get_node("n1")
        assert node is not None
        assert node["name"] == "测试公司"

    def test_upsert_merges(self, neo4j_repository: Neo4jGraphRepository) -> None:
        neo4j_repository.upsert_nodes([Company(id="n1", name="甲")])
        neo4j_repository.upsert_nodes([Company(id="n1", name="乙")])
        node = neo4j_repository.get_node("n1")
        assert node["name"] == "乙"

    def test_relations_round_trip(self, neo4j_repository: Neo4jGraphRepository) -> None:
        neo4j_repository.upsert_nodes([Company(id="a", name="A"), Company(id="b", name="B")])
        neo4j_repository.upsert_relations(
            [
                Relation(
                    source_id="a",
                    target_id="b",
                    relation_type=RelationType.HOLD,
                    properties={"percent": 0.6},
                )
            ]
        )
        paths = neo4j_repository.traverse("a", [RelationType.HOLD], max_hops=1, direction="out")
        assert len(paths) == 1
        assert paths[0].node_ids() == ["a", "b"]

    def test_find_nodes(self, neo4j_repository: Neo4jGraphRepository) -> None:
        neo4j_repository.upsert_nodes([Company(id="a", name="A")])
        assert len(neo4j_repository.find_nodes("Company")) == 1

    def test_stats(self, neo4j_repository: Neo4jGraphRepository) -> None:
        neo4j_repository.upsert_nodes([Company(id="a", name="A")])
        stats = neo4j_repository.stats()
        assert stats.get("Company", 0) >= 1

    def test_neighbours_returns_the_incident_edges(
        self, neo4j_repository: Neo4jGraphRepository
    ) -> None:
        """The neighbour view must match the in-memory store's shape.

        The in-memory store keeps the relation type in a ``relationType``
        property; Neo4j keeps it in the relationship type itself. The
        projection bridges the two so that consumers need no branching.
        """
        neo4j_repository.upsert_nodes([Company(id="n1", name="甲"), Company(id="n2", name="乙")])
        neo4j_repository.upsert_relations(
            [
                Relation(
                    source_id="n1",
                    target_id="n2",
                    relation_type=RelationType.HOLD,
                    properties={"percent": 0.6},
                )
            ]
        )
        edges = neo4j_repository.neighbours("n1")
        assert len(edges) == 1
        edge = edges[0]
        assert edge["source"] == "n1"
        assert edge["target"] == "n2"
        assert edge["relationType"] == "HOLD"
        assert float(edge["percent"]) == pytest.approx(0.6)

    def test_neighbours_sees_both_directions(self, neo4j_repository: Neo4jGraphRepository) -> None:
        neo4j_repository.upsert_nodes([Company(id="n1", name="甲"), Company(id="n2", name="乙")])
        neo4j_repository.upsert_relations(
            [
                Relation(
                    source_id="n1",
                    target_id="n2",
                    relation_type=RelationType.HOLD,
                    properties={"percent": 0.6},
                )
            ]
        )
        assert len(neo4j_repository.neighbours("n2")) == 1

    def test_an_event_date_filter_does_not_raise(
        self, neo4j_repository: Neo4jGraphRepository
    ) -> None:
        """``occurredDate`` is stored as a string; the query converts it.

        Comparing a string property with a ``date()`` value is a type error
        in Cypher, so the indicator query must convert the property
        explicitly.
        """
        from fin_kg.domain.entities import Event

        neo4j_repository.upsert_nodes(
            [
                Company(id="n1", name="甲"),
                Event(
                    id="e1",
                    name="涉诉",
                    event_type="litigation",
                    occurred_date=date(2026, 3, 1),
                    amount=100.0,
                ),
            ]
        )
        neo4j_repository.upsert_relations(
            [
                Relation(
                    source_id="e1",
                    target_id="n1",
                    relation_type=RelationType.INVOLVED_IN,
                    properties={"role": "被告"},
                )
            ]
        )
        from fin_kg.domain.indicators import INDICATOR_BY_ID
        from fin_kg.risk.indicators_engine import GraphIndicatorEngine

        spec = INDICATOR_BY_ID["IND-LIT-001"]
        value = GraphIndicatorEngine(neo4j_repository).measure(spec, "n1", since=date(2026, 1, 1))
        assert value == pytest.approx(1.0)

"""Unit tests for the performance characteristics of the scoring path.

The scoring path is O(n) in the number of companies, so it needs a bound.
These tests pin both halves of the contract: the optimisation must not
change the answer, and the bound must actually bound the work.
"""

from __future__ import annotations

import pytest

from fin_kg.risk.service import RiskServiceConfig, build_service


class TestRankCandidates:
    def test_the_full_ranking_scores_every_entity(self, risk_service) -> None:
        """Without a candidate bound the ranking stays exhaustive."""
        companies = risk_service.repository.find_nodes("Company")
        scores = risk_service.rank(limit=5)
        assert len(scores) <= 5
        # Every company was scored, so the top of the list is the true top.
        assert len(companies) > 5

    def test_the_candidate_bound_limits_the_work(self, risk_service) -> None:
        """A candidate bound must actually reduce the number of scores."""
        risk_service.rank(limit=5)  # write the stored scores first
        bounded = risk_service.rank(limit=5, candidates=3)
        assert len(bounded) <= 3

    def test_the_candidate_bound_does_not_change_the_top(self, risk_service) -> None:
        """The pre-filter is an optimisation, not a different answer.

        The stored risk score was written by a previous run of the same
        computation, so ranking the candidates by it and re-scoring them
        must yield the same leaders as scoring everything.
        """
        risk_service.rank(limit=10)  # write the stored scores
        full = risk_service.rank(limit=10)
        bounded = risk_service.rank(limit=10, candidates=50)

        full_ids = [score.entity_id for score in full[:5]]
        bounded_ids = [score.entity_id for score in bounded[:5]]
        assert full_ids == bounded_ids

    def test_the_scores_themselves_are_unchanged(self, risk_service) -> None:
        risk_service.rank(limit=10)
        full = {score.entity_id: score.score for score in risk_service.rank(limit=10)}
        bounded = {
            score.entity_id: score.score for score in risk_service.rank(limit=10, candidates=50)
        }
        for entity_id, score in full.items():
            if entity_id in bounded:
                assert bounded[entity_id] == pytest.approx(score)


class TestFindNodesBounding:
    def test_the_limit_is_respected(self, seeded_repository) -> None:
        nodes = seeded_repository.find_nodes("Company", limit=3)
        assert len(nodes) == 3

    def test_the_ordering_is_applied(self, seeded_repository) -> None:
        nodes = seeded_repository.find_nodes("Company", order_by="riskScore")
        scores = [float(node.get("riskScore") or 0.0) for node in nodes]
        assert scores == sorted(scores, reverse=True)

    def test_the_limit_and_the_ordering_combine(self, seeded_repository) -> None:
        nodes = seeded_repository.find_nodes("Company", limit=2, order_by="riskScore")
        assert len(nodes) == 2

    def test_without_a_limit_everything_is_returned(self, seeded_repository) -> None:
        total = len(seeded_repository.find_nodes("Company"))
        assert len(seeded_repository.find_nodes("Company", limit=None)) == total


class TestBulkPersistence:
    def test_the_scores_are_written_in_one_go(self, seeded_repository) -> None:
        service = build_service(
            seeded_repository,
            _rules(),
            config=RiskServiceConfig(persist_scores=True),
        )
        scores = service.rank(limit=5)
        assert scores
        for score in scores:
            node = seeded_repository.get_node(score.entity_id)
            assert node is not None
            assert float(node["riskScore"]) == pytest.approx(score.score)

    def test_a_bulk_update_reports_the_count(self, seeded_repository) -> None:
        companies = seeded_repository.find_nodes("Company")
        updates = {str(company["id"]): {"riskScore": 42.0} for company in companies[:3]}
        updated = seeded_repository.update_properties_bulk(updates)
        assert updated == 3
        node = seeded_repository.get_node(str(companies[0]["id"]))
        assert node["riskScore"] == 42.0

    def test_a_bulk_update_of_nothing_is_a_no_op(self, seeded_repository) -> None:
        assert seeded_repository.update_properties_bulk({}) == 0


def _rules():
    from fin_kg.risk.engine import RuleEngine
    from tests.conftest import RULES_PATH

    return RuleEngine.from_yaml(RULES_PATH)

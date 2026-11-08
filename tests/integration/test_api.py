"""Integration tests of the HTTP API.

These tests exercise the full request → service → graph → response path
using the in-memory store, so they run without any external service. The
Neo4j-specific behaviour is covered separately in
``test_neo4j_integration.py`` and is skipped when no Neo4j is reachable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fin_kg.ingestion.pipeline import IngestionPipeline
from fin_kg.ingestion.sources import SyntheticSourceAdapter


@pytest.fixture
def seeded_client(app_with_repository: TestClient) -> TestClient:
    """A client whose graph already holds the synthetic dataset."""
    from fin_kg.ingestion.pipeline import IngestionPipeline
    from fin_kg.ingestion.sources import SyntheticSourceAdapter

    repository = app_with_repository.repository  # type: ignore[attr-defined]
    IngestionPipeline(repository).run(
        [SyntheticSourceAdapter(companies=25, events_per_company=2.0, seed=11)]
    )
    return app_with_repository


class TestHealth:
    def test_root_returns_the_banner(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["name"] == "fin-kg-risk-engine"

    def test_health_is_ok(self, client: TestClient) -> None:
        payload = client.get("/health").json()
        assert payload["status"] == "ok"
        assert "graph_store" in payload["dependencies"]

    def test_ready(self, client: TestClient) -> None:
        assert client.get("/ready").status_code == 200

    def test_metrics_are_exposed(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "fin_kg_http_requests_total" in response.text

    def test_openapi_document_is_valid(self, client: TestClient) -> None:
        document = client.get("/openapi.json").json()
        assert document["info"]["title"] == "fin-kg-risk-engine"
        assert len(document["paths"]) >= 15


class TestGraphEndpoints:
    def test_stats(self, seeded_client: TestClient) -> None:
        payload = seeded_client.get("/api/v1/graph/stats").json()
        assert payload["totalNodes"] > 0
        assert payload["nodes"]["Company"] > 0

    def test_entity_list_is_paginated(self, seeded_client: TestClient) -> None:
        payload = seeded_client.get(
            "/api/v1/graph/entities", params={"label": "Company", "limit": 5}
        ).json()
        assert len(payload["items"]) <= 5
        assert payload["limit"] == 5

    def test_entity_detail(self, seeded_client: TestClient) -> None:
        companies = seeded_client.get("/api/v1/graph/entities").json()["items"]
        entity_id = companies[0]["id"]
        response = seeded_client.get(f"/api/v1/graph/entities/{entity_id}")
        assert response.status_code == 200
        assert response.json()["id"] == entity_id

    def test_missing_entity_returns_404(self, seeded_client: TestClient) -> None:
        response = seeded_client.get("/api/v1/graph/entities/does-not-exist")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "not_found"

    def test_neighbourhood(self, seeded_client: TestClient) -> None:
        companies = seeded_client.get("/api/v1/graph/entities").json()["items"]
        entity_id = companies[0]["id"]
        response = seeded_client.get(
            f"/api/v1/graph/entities/{entity_id}/neighbourhood", params={"hops": 2}
        )
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestRiskEndpoints:
    def test_entity_score(self, seeded_client: TestClient) -> None:
        companies = seeded_client.get("/api/v1/graph/entities").json()["items"]
        entity_id = companies[0]["id"]
        payload = seeded_client.get(f"/api/v1/risk/entities/{entity_id}/score").json()
        assert 0 <= payload["score"] <= 100
        assert payload["readings"]
        assert payload["explanation"]

    def test_score_of_missing_entity_returns_404(self, seeded_client: TestClient) -> None:
        response = seeded_client.get("/api/v1/risk/entities/nope/score")
        assert response.status_code == 404

    def test_risk_rank(self, seeded_client: TestClient) -> None:
        payload = seeded_client.get("/api/v1/risk/rank", params={"limit": 5}).json()
        assert len(payload["items"]) <= 5
        scores = [item["score"] for item in payload["items"]]
        assert scores == sorted(scores, reverse=True)

    def test_alerts(self, seeded_client: TestClient) -> None:
        payload = seeded_client.get("/api/v1/risk/alerts").json()
        assert "items" in payload
        assert "severities" in payload

    def test_alerts_reject_an_invalid_severity(self, seeded_client: TestClient) -> None:
        response = seeded_client.get(
            "/api/v1/risk/alerts", params={"min_severity": "nonsense"}
        )
        assert response.status_code == 422

    def test_entity_exposure(self, seeded_client: TestClient) -> None:
        companies = seeded_client.get("/api/v1/graph/entities").json()["items"]
        entity_id = companies[0]["id"]
        payload = seeded_client.get(f"/api/v1/risk/entities/{entity_id}/exposure").json()
        assert "affected" in payload


class TestResearchEndpoints:
    def test_ask_returns_an_answer_with_evidence(self, seeded_client: TestClient) -> None:
        companies = seeded_client.get("/api/v1/graph/entities").json()["items"]
        name = companies[0]["name"]
        payload = seeded_client.post(
            "/api/v1/research/ask", json={"question": f"{name}的股权结构是什么？"}
        ).json()
        assert payload["answer"]
        assert payload["intent"] == "holding_structure"

    def test_ask_rejects_an_empty_question(self, seeded_client: TestClient) -> None:
        response = seeded_client.post("/api/v1/research/ask", json={"question": ""})
        assert response.status_code in (422, 500)

    def test_report(self, seeded_client: TestClient) -> None:
        companies = seeded_client.get("/api/v1/graph/entities").json()["items"]
        entity_id = companies[0]["id"]
        payload = seeded_client.get(f"/api/v1/research/report/{entity_id}").json()
        assert payload["markdown"]
        assert payload["sections"]

    def test_similar_cases(self, seeded_client: TestClient) -> None:
        companies = seeded_client.get("/api/v1/graph/entities").json()["items"]
        entity_id = companies[0]["id"]
        response = seeded_client.get(f"/api/v1/research/similar/{entity_id}")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestJobEndpoints:
    def test_ingest_job_reports_progress(self, client: TestClient) -> None:
        response = client.post("/api/v1/jobs/ingest", json={"source": "synthetic"})
        assert response.status_code == 202
        payload = response.json()
        assert payload["jobId"]
        assert payload["jobType"] == "ingest"

    def test_job_status_is_pollable(self, client: TestClient) -> None:
        job_id = client.post("/api/v1/jobs/ingest", json={"source": "synthetic"}).json()[
            "jobId"
        ]
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        assert response.json()["jobId"] == job_id

    def test_unknown_job_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/jobs/nope").status_code == 404

    def test_unknown_source_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/v1/jobs/ingest", json={"source": "nonexistent"})
        assert response.status_code in (422, 500)

    def test_job_list(self, client: TestClient) -> None:
        client.post("/api/v1/jobs/ingest", json={"source": "synthetic"})
        assert isinstance(client.get("/api/v1/jobs").json(), list)


class TestErrorEnvelope:
    def test_errors_use_a_stable_envelope(self, seeded_client: TestClient) -> None:
        response = seeded_client.get("/api/v1/graph/entities/missing")
        body = response.json()
        assert "code" in body["detail"]
        assert "message" in body["detail"]

    def test_request_id_is_returned(self, client: TestClient) -> None:
        response = client.get("/health", headers={"X-Request-ID": "test-request-id"})
        assert response.headers.get("X-Request-ID") == "test-request-id"

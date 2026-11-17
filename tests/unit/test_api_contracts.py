"""Wire-contract tests for the two new endpoints (R-02, R-07).

The wrappers are deliberately thin: these tests pin the response shape, the
byte-for-byte report body and the filename safety rules, without touching
the scoring model itself.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import unquote

from fastapi.testclient import TestClient

from fin_kg.api import deps
from fin_kg.domain.entities import Company
from fin_kg.domain.indicators import RISK_INDICATORS
from fin_kg.risk.indicators_engine import IndicatorEngine
from fin_kg.risk.service import RiskService, RiskServiceConfig


class _StubIndicatorEngine(IndicatorEngine):
    """Return a controlled subset of measurements for the coverage cases."""

    def __init__(self, measured: dict[str, float]) -> None:
        self.measured = measured

    def measure(self, spec, entity_id: str, *, since: date) -> float | None:
        return self.measured.get(spec.id)


def _client_with_measurements(
    app_with_repository: TestClient, measured: dict[str, float]
) -> TestClient:
    """Override the risk service with one backed by a stub indicator engine."""
    repository = app_with_repository.repository  # type: ignore[attr-defined]
    repository.upsert_nodes(
        [
            Company(id="c1", name="甲公司"),
            Company(id="c2", name="甲/乙公司"),
        ]
    )
    service = RiskService(
        repository=repository,
        rules=deps.get_rules(),
        indicator_engine=_StubIndicatorEngine(measured),
        config=RiskServiceConfig(persist_scores=False),
    )
    app_with_repository.app.dependency_overrides[deps.get_risk_service] = lambda: service
    return app_with_repository


class TestCoverageEndpoint:
    def test_every_indicator_measured(self, app_with_repository: TestClient) -> None:
        measured = {spec.id: 1.0 for spec in RISK_INDICATORS}
        client = _client_with_measurements(app_with_repository, measured)
        payload = client.get("/api/v1/risk/entities/c1/coverage").json()
        assert payload["entityId"] == "c1"
        assert payload["coverage"] == 1.0
        assert payload["totalIndicators"] == len(RISK_INDICATORS)
        assert payload["coveredIndicators"] == len(RISK_INDICATORS)
        assert payload["missingIndicatorIds"] == []

    def test_three_missing_indicators(self, app_with_repository: TestClient) -> None:
        missing_ids = {spec.id for spec in RISK_INDICATORS[:3]}
        measured = {spec.id: 1.0 for spec in RISK_INDICATORS if spec.id not in missing_ids}
        client = _client_with_measurements(app_with_repository, measured)
        payload = client.get("/api/v1/risk/entities/c1/coverage").json()
        assert payload["coveredIndicators"] == len(RISK_INDICATORS) - 3
        assert set(payload["missingIndicatorIds"]) == missing_ids
        assert payload["coverage"] == round((len(RISK_INDICATORS) - 3) / len(RISK_INDICATORS), 3)

    def test_missing_entity_returns_the_flat_404_envelope(
        self, app_with_repository: TestClient
    ) -> None:
        client = _client_with_measurements(app_with_repository, {})
        response = client.get("/api/v1/risk/entities/nope/coverage")
        assert response.status_code == 404
        body = response.json()
        assert "code" in body and "message" in body
        assert set(body) <= {"code", "message", "details"}
        assert "detail" not in body

    def test_score_is_identical_before_and_after_coverage(
        self, app_with_repository: TestClient
    ) -> None:
        client = _client_with_measurements(app_with_repository, {})
        before = client.get("/api/v1/risk/entities/c1/score").json()["score"]
        client.get("/api/v1/risk/entities/c1/coverage")
        after = client.get("/api/v1/risk/entities/c1/score").json()["score"]
        assert after == before

    def test_coverage_fields_are_not_added_to_the_score_response(
        self, app_with_repository: TestClient
    ) -> None:
        client = _client_with_measurements(app_with_repository, {})
        score = client.get("/api/v1/risk/entities/c1/score").json()
        assert "coverage" not in score
        assert "coveredIndicators" not in score
        assert "missingIndicatorIds" not in score

    def test_endpoint_is_in_the_openapi_document(self, app_with_repository: TestClient) -> None:
        client = _client_with_measurements(app_with_repository, {})
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/risk/entities/{entity_id}/coverage" in paths


class TestReportDownload:
    def test_body_matches_report_byte_for_byte(self, app_with_repository: TestClient) -> None:
        client = _client_with_measurements(app_with_repository, {})
        report = client.get("/api/v1/research/report/c1").json()
        response = client.get("/api/v1/research/report/c1/download")
        assert response.status_code == 200
        assert response.text.encode("utf-8") == report["markdown"].encode("utf-8")
        assert response.headers["content-type"].startswith("text/markdown; charset=utf-8")

        disposition = response.headers["content-disposition"]
        assert 'filename="c1.md"' in disposition
        assert "filename*=UTF-8''" in disposition
        encoded_name = disposition.split("filename*=UTF-8''", 1)[1]
        assert unquote(encoded_name) == "甲公司.md"

    def test_path_separators_are_sanitized(self, app_with_repository: TestClient) -> None:
        client = _client_with_measurements(app_with_repository, {})
        response = client.get("/api/v1/research/report/c2/download")
        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        assert "/" not in disposition and "\\" not in disposition
        encoded_name = disposition.split("filename*=UTF-8''", 1)[1]
        assert unquote(encoded_name) == "甲_乙公司.md"

    def test_missing_entity_returns_the_flat_404_envelope(
        self, app_with_repository: TestClient
    ) -> None:
        client = _client_with_measurements(app_with_repository, {})
        response = client.get("/api/v1/research/report/nope/download")
        assert response.status_code == 404
        body = response.json()
        assert "code" in body and "message" in body
        assert set(body) <= {"code", "message", "details"}
        assert "detail" not in body

    def test_endpoint_is_in_the_openapi_document(self, app_with_repository: TestClient) -> None:
        client = _client_with_measurements(app_with_repository, {})
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/research/report/{entity_id}/download" in paths

"""Independent QA verification for engineering round 7.

These tests are authored by QA, not by the implementer. They deliberately
target the boundaries the implementation report claims to have fixed:
injection payloads that must never reach Cypher, cross-engine consistency,
wire compatibility, and the filename-safety of the download endpoint.
"""

from __future__ import annotations

import re
import sys
import types
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
import tomllib
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fin_kg.api import deps
from fin_kg.core.errors import GraphStoreError
from fin_kg.domain.entities import (
    Company,
    Event,
    EventType,
)
from fin_kg.domain.relations import Relation, RelationType
from fin_kg.graph.repository import (
    VALID_NODE_LABELS,
    VALID_ORDER_COLUMNS,
    FileGraphRepository,
    InMemoryGraphRepository,
    Neo4jGraphRepository,
)
from fin_kg.risk.engine import RuleEngine
from fin_kg.risk.indicators_engine import (
    GraphIndicatorEngine,
    IndicatorEngine,
    LocalIndicatorEngine,
    window_start,
)
from fin_kg.risk.service import RiskService, RiskServiceConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# A01 — injection guard
# ---------------------------------------------------------------------------


class _SpyNeo4j(Neo4jGraphRepository):
    """A Neo4j repository that records queries instead of talking to a server."""

    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        super().__init__(object())
        self.executed: list[str] = []

    def _run(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.executed.append(cypher)
        return []


MALICIOUS_LABELS = [
    "Company) DETACH DELETE n MATCH (m:Company",
    "Company'} DETACH DELETE n //",
    "Company` DETACH DELETE n //",
    "company",  # whitelist is case-sensitive
    "Company\nDETACH DELETE n",
    "",
]

MALICIOUS_ORDER_BY = [
    "riskScore; DETACH DELETE n",
    "riskScore DESC, n.name",
    "riskScore) DETACH DELETE n //",
    "RiskScore",
    "",
]


@pytest.mark.parametrize("payload", MALICIOUS_LABELS)
def test_a01_malicious_label_never_reaches_the_driver(payload: str) -> None:
    """The Neo4j driver must not see a single statement for a bad label."""
    store = _SpyNeo4j()
    with pytest.raises(GraphStoreError):
        store.find_nodes(payload)
    assert store.executed == [], f"injection reached Cypher: {store.executed}"


@pytest.mark.parametrize("payload", MALICIOUS_ORDER_BY)
def test_a01_malicious_order_by_never_reaches_the_driver(payload: str) -> None:
    store = _SpyNeo4j()
    with pytest.raises(GraphStoreError):
        store.find_nodes("Company", order_by=payload)
    assert store.executed == [], f"injection reached Cypher: {store.executed}"


def test_a01_all_three_repository_flavours_reject_the_same_payload(
    repository: InMemoryGraphRepository, tmp_path: Path
) -> None:
    file_store = FileGraphRepository(tmp_path / "graph.json", autosave=False)
    file_store.ensure_schema()
    for store in (repository, file_store, _SpyNeo4j()):
        with pytest.raises(GraphStoreError):
            store.find_nodes("Company) DETACH DELETE n")
        with pytest.raises(GraphStoreError):
            store.find_nodes("Company", order_by="riskScore; DROP n")


def test_a01_legal_whitelist_values_still_work_everywhere(
    repository: InMemoryGraphRepository, tmp_path: Path
) -> None:
    """The guard must not turn a legitimate query into a regression."""
    from fin_kg.domain.entities import Person, Product

    repository.upsert_nodes(
        [
            Company(id="c", name="公司"),
            Person(id="p", name="人"),
            Event(id="e", name="事件"),
            Product(id="d", name="产品"),
        ]
    )
    for label in sorted(VALID_NODE_LABELS):
        assert isinstance(repository.find_nodes(label), list)
    for column in sorted(VALID_ORDER_COLUMNS):
        # The guard admits all three identifiers; the offline store can only
        # sort numerically, so validate the guard itself for timestamp keys
        # and exercise real sorting on the key production code uses.
        repository._validate_order_by(column)
    assert [node["id"] for node in repository.find_nodes("Company", order_by="riskScore")] == ["c"]

    file_store = FileGraphRepository(tmp_path / "graph.json", autosave=False)
    file_store.ensure_schema()
    file_store.upsert_nodes([Company(id="c", name="公司")])
    assert [node["id"] for node in file_store.find_nodes("Company")] == ["c"]

    store = _SpyNeo4j()
    store.find_nodes("Company", order_by="riskScore", limit=5)
    assert store.executed == [
        "MATCH (n:Company) WHERE all(k IN keys($filters) WHERE n[k] = $filters[k]) "
        "RETURN n{.*} AS node ORDER BY n.riskScore DESC LIMIT 5"
    ]


def test_a01_execute_read_keeps_the_original_case_of_the_label(
    repository: InMemoryGraphRepository,
) -> None:
    """Regression for the reported `lower()` slicing fix."""
    repository.upsert_nodes([Company(id="c1", name="甲"), Event(id="e1", name="事件")])
    rows = repository.execute_read("MATCH (n:Company) RETURN n", {})
    assert [row["id"] for row in rows] == ["c1"]
    rows = repository.execute_read("  MATCH   (n:Event)   RETURN n  ", {})
    assert [row["id"] for row in rows] == ["e1"]
    # A label that is not on the whitelist is still rejected here. The
    # offline interpreter only recognises the exact `MATCH (n:X) ... RETURN n`
    # shape; arbitrary Cypher is rejected rather than silently executed.
    with pytest.raises(GraphStoreError):
        repository.execute_read("MATCH (n:Evil) RETURN n", {})


def test_a01_api_rejects_a_malicious_label_with_the_flat_error_envelope(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/graph/entities",
        params={"label": "Company) DETACH DELETE n MATCH (m:Company"},
    )
    assert response.status_code == 422
    assert set(response.json()) <= {"code", "message", "details"}


def test_a01_api_accepts_every_whitelisted_label(client: TestClient) -> None:
    for label in sorted(VALID_NODE_LABELS):
        response = client.get("/api/v1/graph/entities", params={"label": label})
        assert response.status_code == 200, f"{label}: {response.text}"


# ---------------------------------------------------------------------------
# A02 / A03 / A06 — graph counting and persistence
# ---------------------------------------------------------------------------


def test_a02_total_nodes_is_deduplicated(client: TestClient) -> None:
    repository: InMemoryGraphRepository = client.repository  # type: ignore[attr-defined]
    repository.upsert_nodes([Company(id="a", name="甲"), Company(id="b", name="乙")])
    stats = client.get("/api/v1/graph/stats").json()
    assert stats["nodes"]["Entity"] == 2
    assert stats["nodes"]["Company"] + stats["nodes"]["Entity"] == 4
    assert stats["totalNodes"] == 2
    assert client.get("/health").json()["dependencies"]["nodeCount"] == "2"


def test_a03_bulk_update_writes_the_file_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileGraphRepository(tmp_path / "graph.json", autosave=True)
    store.ensure_schema()
    store.upsert_nodes([Company(id=f"c{i}", name=f"公司{i}") for i in range(20)])

    original_save = store.save
    saves: list[None] = []

    def counted_save() -> None:
        saves.append(None)
        original_save()

    monkeypatch.setattr(store, "save", counted_save)
    updates = {f"c{i}": {"riskScore": float(i)} for i in range(5)}
    assert store.update_properties_bulk(updates) == 5
    assert len(saves) == 1, f"expected one file write, got {len(saves)}"
    monkeypatch.undo()
    assert store.update_properties_bulk({"missing": {"riskScore": 1.0}}) == 0


def test_a03_bulk_update_does_not_write_when_autosave_is_off(tmp_path: Path) -> None:
    store = FileGraphRepository(tmp_path / "off.json", autosave=False)
    store.ensure_schema()
    store.upsert_nodes([Company(id="c1", name="甲")])
    assert store.update_properties_bulk({"c1": {"riskScore": 7.0}}) == 1
    assert not (tmp_path / "off.json").exists()


def test_a06_detail_labels_carry_the_concrete_type(client: TestClient) -> None:
    repository: InMemoryGraphRepository = client.repository  # type: ignore[attr-defined]
    repository.upsert_nodes([Company(id="c1", name="甲")])
    payload = client.get("/api/v1/graph/entities/c1").json()
    assert set(payload["labels"]) == {"Entity", "Company"}


# ---------------------------------------------------------------------------
# A05 / A07 / A08 / A09 / A10 — risk and observability
# ---------------------------------------------------------------------------


def test_a05_rules_loaded_gauge_follows_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin_kg.observability.metrics import REGISTRY

    deps.reset_dependencies()
    ok = deps.get_rules()
    assert REGISTRY.get_sample_value("fin_kg_rules_loaded") == float(len(ok.rules))
    assert ok.rules

    broken = tmp_path / "broken.yaml"
    broken.write_text("rules: [ {invalid: ", encoding="utf-8")

    class _Settings:
        risk = types.SimpleNamespace(rules_path=Path("unused.yaml"))

        def resolve_path(self, _: Path) -> Path:
            return broken

    monkeypatch.setattr(deps, "get_settings", lambda: _Settings())
    deps.reset_dependencies()
    degraded = deps.get_rules()
    assert degraded.rules == []
    assert REGISTRY.get_sample_value("fin_kg_rules_loaded") == 0.0
    monkeypatch.undo()
    deps.reset_dependencies()


def test_a07_affected_entities_are_first_reach_deduplicated() -> None:
    engine = RuleEngine.from_payload(
        {
            "rules": [
                {
                    "id": "R-QA",
                    "name": "path",
                    "severity": "warning",
                    "condition": {
                        "all": [
                            {
                                "path": {
                                    "relation_types": ["GUARANTEE"],
                                    "min_hops": 1,
                                    "min_paths": 1,
                                }
                            }
                        ]
                    },
                    "message": "hit",
                }
            ]
        }
    )

    from fin_kg.domain.risk import RiskScore, TransmissionPath

    def path(*ids: str) -> Any:
        return TransmissionPath(
            node_ids=list(ids),
            node_names=[f"N{x}" for x in ids],
            relation_types=["GUARANTEE"] * (len(ids) - 1),
            transmissibilities=[1.0] * (len(ids) - 1),
            propagated_score=40.0,
            hops=len(ids) - 1,
        )

    paths = [path("a", "b", "d"), path("a", "c", "d"), path("a", "b", "c", "d")]
    assessment = engine.evaluate_assessment(RiskScore(entity_id="a", score=40), paths)
    assert [item["id"] for item in assessment.affected_entities] == ["b", "d", "c"]
    assert [item["hops"] for item in assessment.affected_entities] == [1, 2, 1]
    assert len(assessment.transmission_paths) == 3
    alerts = engine.evaluate(RiskScore(entity_id="a", score=40), paths)
    assert alerts[0].affected_entity_ids == ["b", "d", "c"]


def test_a08_description_changed_without_touching_the_measurement() -> None:
    from fin_kg.domain.indicators import INDICATOR_BY_ID

    spec = INDICATOR_BY_ID["IND-NEG-001"]
    assert spec.description == "近 12 个月负面舆情事件数量"
    assert spec.weight == 0.05
    assert spec.thresholds == ((0, 0.0), (3, 40.0), (10, 70.0), (25, 100.0))
    assert spec.measurement_cypher == (
        "MATCH (c:Company {id:$id})-[:INVOLVED_IN]->(e:Event) "
        "WHERE e.eventType='negative_news' AND date(e.occurredDate) >= date($since) "
        "RETURN count(e) AS value"
    )


class _CypherWindowRepository(InMemoryGraphRepository):
    """Reference model of the Cypher WHERE clause used by the event indicators.

    ``date(e.occurredDate) >= date($since)`` evaluates to NULL — and is
    therefore filtered out — when ``occurredDate`` is absent. This adapter
    implements exactly that documented Cypher semantics against the same
    snapshot, so the two engines can be compared without a live Neo4j.
    """

    def execute_read(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        text = " ".join(cypher.split())
        if "MATCH (c:Company" not in text or "RETURN" not in text:
            raise GraphStoreError("unsupported reference query", details={"cypher": cypher})
        params = parameters or {}
        entity_id = str(params.get("id"))
        since = str(params.get("since"))
        wanted = re.findall(r"'([^']+)'", text)
        if not wanted:
            raise GraphStoreError("no event type in reference query", details={"cypher": cypher})

        kept: list[dict[str, Any]] = []
        for edge in self.neighbours(entity_id):
            if edge.get("relationType") != "INVOLVED_IN":
                continue
            event = self.get_node(edge["target"]) or {}
            if event.get("eventType") not in wanted:
                continue
            occurred = event.get("occurredDate")
            if not occurred or str(occurred) < since:
                continue  # Cypher: date(NULL) >= date($since) -> NULL -> excluded
            kept.append(event)

        if "sum(e.amount)" in text:
            capital = float((self.get_node(entity_id) or {}).get("regCapital") or 0.0)
            exposure = sum(float(event.get("amount") or 0.0) for event in kept)
            value = exposure / capital if capital > 0 else 0.0
        else:
            value = float(len(kept))
        return [{"value": value}]


def test_a09_local_and_cypher_engines_agree_on_undated_events() -> None:
    today = date(2026, 6, 1)
    since = window_start(365, today=today)
    events = [
        Event(
            id="e1",
            name="窗口内诉讼",
            event_type=EventType.LITIGATION,
            occurred_date=today - timedelta(days=10),
        ),
        Event(id="e2", name="无日期诉讼", event_type=EventType.LITIGATION),
        Event(
            id="e3",
            name="超窗诉讼",
            event_type=EventType.LITIGATION,
            occurred_date=today - timedelta(days=400),
        ),
        Event(
            id="e4",
            name="窗口内执行",
            event_type=EventType.ENFORCEMENT,
            occurred_date=today - timedelta(days=5),
            amount=50.0,
        ),
        Event(id="e5", name="无日期执行", event_type=EventType.ENFORCEMENT, amount=999.0),
        Event(
            id="e6",
            name="窗口内处罚",
            event_type=EventType.PENALTY,
            occurred_date=today - timedelta(days=2),
        ),
        Event(
            id="e7",
            name="窗口内降级",
            event_type=EventType.RATING_DOWNGRADE,
            occurred_date=today - timedelta(days=1),
        ),
        Event(id="e8", name="无日期负面舆情", event_type=EventType.NEGATIVE_NEWS),
    ]
    store = _CypherWindowRepository()
    store.ensure_schema()
    store.upsert_nodes([Company(id="c1", name="甲", reg_capital=100.0), *events])
    store.upsert_relations(
        [
            Relation(source_id="c1", target_id=event.id, relation_type=RelationType.INVOLVED_IN)
            for event in events
        ]
    )

    from fin_kg.domain.indicators import INDICATOR_BY_ID

    local = LocalIndicatorEngine(store)
    graph = GraphIndicatorEngine(store)
    expected = {
        "IND-LIT-001": 2.0,
        "IND-PEN-001": 1.0,
        "IND-RTE-001": 1.0,
        "IND-NEG-001": 0.0,
        "IND-DEF-001": 0.0,
        "IND-ENF-001": 0.5,
    }
    for indicator_id, value in expected.items():
        spec = INDICATOR_BY_ID[indicator_id]
        local_value = local.measure(spec, "c1", since=since)
        graph_value = graph.measure(spec, "c1", since=since)
        assert local_value == graph_value, (
            f"{indicator_id}: local={local_value}, cypher={graph_value}"
        )
        assert local_value == pytest.approx(value), indicator_id


def test_a10_gnn_severity_is_declared_validated_and_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fin_kg.cli import args_min_severity
    from fin_kg.config.settings import GnnSettings, reset_settings_cache

    assert GnnSettings().label_severity == "major"

    monkeypatch.setenv("FIN_KG_GNN_LABEL_SEVERITY", "critical")
    reset_settings_cache()
    assert args_min_severity() == "critical"

    monkeypatch.setenv("FIN_KG_GNN_LABEL_SEVERITY", "not-a-severity")
    reset_settings_cache()
    with pytest.raises(ValidationError):
        args_min_severity()
    reset_settings_cache()


def test_a11_no_stale_test_counts_in_the_docs() -> None:
    for relative in (
        "README.md",
        "README.en.md",
        "docs/adr/ADR-0002-layered-architecture.md",
    ):
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        assert "407 通过" not in text
        assert "407 passing" not in text
        assert "211 项测试" not in text


def test_a12_coverage_gate_is_eighty() -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["coverage"]["report"]["fail_under"] == 80


def test_a13_fake_schedule_module_does_not_break_the_builtin_cron_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fin_kg.scheduler import runner

    class _BrokenChain:
        def minute(self) -> _BrokenChain:
            return self

        def at(self, value: str) -> None:  # pragma: no cover - must never run
            raise AssertionError("the removed schedule branch was used")

        def should_run(self) -> bool:  # pragma: no cover - must never run
            raise AssertionError("the removed schedule branch was used")

    fake = types.ModuleType("schedule")
    fake.every = lambda: _BrokenChain()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "schedule", fake)

    started: list[Any] = []

    class _Thread:
        def __init__(self, target: Any = None, args: Any = (), daemon: bool | None = None) -> None:
            self.target, self.args = target, args

        def start(self) -> None:
            started.append(self)

    import threading

    monkeypatch.setattr(
        runner,
        "threading",
        types.SimpleNamespace(Thread=_Thread, Event=threading.Event, Lock=threading.Lock),
    )

    calls: list[str] = []
    scheduler = runner.Scheduler()
    scheduler.register(runner.JobDefinition("due", "* * * * *", lambda: calls.append("due")))
    scheduler.register(runner.JobDefinition("idle", "0 0 1 1 *", lambda: calls.append("idle")))

    def stop_immediately(_: float) -> None:
        scheduler._stop.set()

    monkeypatch.setattr(scheduler._stop, "wait", stop_immediately)
    scheduler._loop()  # must not raise even with a broken `schedule` installed

    assert len(started) == 1, f"expected exactly one cron-triggered job, got {len(started)}"
    assert started[0].args[0].name == "due"
    assert runner._cron_matches("* * * * *", runner.datetime.now(runner.timezone.utc))


# ---------------------------------------------------------------------------
# R-02 / R-07 — new endpoints (R-01 / R-06 are out of scope for round 7)
# ---------------------------------------------------------------------------


class _StubIndicatorEngine(IndicatorEngine):
    """A stub that inherits the real readings() wrapper."""

    def __init__(self, measured: dict[str, float]) -> None:
        self.measured = measured

    def measure(self, spec: Any, entity_id: str, *, since: date) -> float | None:
        return self.measured.get(spec.id)


def _coverage_client(app_with_repository: TestClient, measured: dict[str, float]) -> TestClient:
    from fin_kg.domain.indicators import RISK_INDICATORS

    repository: InMemoryGraphRepository = app_with_repository.repository  # type: ignore[attr-defined]
    repository.upsert_nodes([Company(id="c1", name="甲公司")])
    service = RiskService(
        repository=repository,
        rules=deps.get_rules(),
        indicator_engine=_StubIndicatorEngine(measured),  # type: ignore[arg-type]
        config=RiskServiceConfig(persist_scores=False),
    )
    app_with_repository.app.dependency_overrides[deps.get_risk_service] = lambda: service
    assert set(measured) <= {spec.id for spec in RISK_INDICATORS}
    return app_with_repository


def test_r02_coverage_does_not_mutate_the_score_or_the_stored_node(
    app_with_repository: TestClient,
) -> None:
    from fin_kg.domain.indicators import RISK_INDICATORS

    client = _coverage_client(app_with_repository, {spec.id: 1.0 for spec in RISK_INDICATORS})
    before = client.get("/api/v1/risk/entities/c1/score").json()
    repository: InMemoryGraphRepository = client.repository  # type: ignore[attr-defined]
    stored_before = dict(repository.get_node("c1") or {})
    coverage = client.get("/api/v1/risk/entities/c1/coverage")
    after = client.get("/api/v1/risk/entities/c1/score").json()
    assert coverage.status_code == 200, coverage.text
    assert before["score"] == after["score"]
    assert before["level"] == after["level"]
    assert stored_before == repository.get_node("c1")
    assert coverage.json()["coverage"] == 1.0
    assert coverage.json()["missingIndicatorIds"] == []


def test_r02_coverage_partial_missing_and_not_found(app_with_repository: TestClient) -> None:
    from fin_kg.domain.indicators import RISK_INDICATORS

    measured = {spec.id: 1.0 for spec in RISK_INDICATORS[:3]}
    client = _coverage_client(app_with_repository, measured)
    payload = client.get("/api/v1/risk/entities/c1/coverage").json()
    assert payload["coveredIndicators"] == 3
    assert payload["totalIndicators"] == len(RISK_INDICATORS)
    assert set(payload["missingIndicatorIds"]) == {spec.id for spec in RISK_INDICATORS[3:]}
    assert client.get("/api/v1/risk/entities/nope/coverage").status_code == 404


def test_r07_download_body_is_byte_identical_to_the_report_markdown(
    app_with_repository: TestClient,
) -> None:
    repository: InMemoryGraphRepository = app_with_repository.repository  # type: ignore[attr-defined]
    repository.upsert_nodes([Company(id="c1", name="甲公司")])
    service = RiskService(
        repository=repository,
        rules=deps.get_rules(),
        indicator_engine=LocalIndicatorEngine(repository),
        config=RiskServiceConfig(persist_scores=False),
    )
    app_with_repository.app.dependency_overrides[deps.get_risk_service] = lambda: service

    report = app_with_repository.get("/api/v1/research/report/c1").json()
    response = app_with_repository.get("/api/v1/research/report/c1/download")
    assert response.status_code == 200
    assert response.content == report["markdown"].encode("utf-8")
    assert response.headers["content-type"].startswith("text/markdown; charset=utf-8")
    assert 'filename="c1.md"' in response.headers["content-disposition"]
    assert app_with_repository.get("/api/v1/research/report/nope/download").status_code == 404


def test_r07_download_filename_removes_path_separators_from_the_entity_name(
    app_with_repository: TestClient,
) -> None:
    """A decoded RFC 5987 filename must not become a path on the client."""
    repository: InMemoryGraphRepository = app_with_repository.repository  # type: ignore[attr-defined]
    repository.upsert_nodes([Company(id="evil", name="A/B\\C 公司")])
    service = RiskService(
        repository=repository,
        rules=deps.get_rules(),
        indicator_engine=LocalIndicatorEngine(repository),
        config=RiskServiceConfig(persist_scores=False),
    )
    app_with_repository.app.dependency_overrides[deps.get_risk_service] = lambda: service
    response = app_with_repository.get("/api/v1/research/report/evil/download")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    decoded = unquote(encoded)
    assert "/" not in decoded, f"unescaped path separator in filename: {decoded!r}"
    assert "\\" not in decoded, f"unescaped path separator in filename: {decoded!r}"

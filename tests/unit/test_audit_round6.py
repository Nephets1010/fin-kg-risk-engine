"""Regression tests for the defects found in the sixth audit round.

They guard three contracts.

- The rule file and the indicator catalogue must agree: every rule must
  reference a real indicator, and every threshold a rule demands must be
  reachable in that indicator's value domain. A critical rule that can
  never fire is a blind spot in the risk system, not a conservative
  setting.
- Credentials must reach the platform through the settings model, so they
  are declared, validated and masked like everything else.
- The graph schema and the documentation must describe the same indexes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fin_kg.domain.indicators import INDICATOR_BY_ID, RISK_INDICATORS
from fin_kg.domain.risk import IndicatorReading, RiskLevel, RiskScore
from fin_kg.risk.engine import RuleEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = PROJECT_ROOT / "rules" / "risk_rules.yaml"


@pytest.fixture(scope="module")
def rules() -> RuleEngine:
    return RuleEngine.from_yaml(RULES_PATH)


def _reading(indicator_id: str, measurement: float) -> IndicatorReading:
    spec = INDICATOR_BY_ID[indicator_id]
    return IndicatorReading(
        indicator_id=spec.id,
        indicator_name=spec.name,
        version=spec.version,
        category=spec.category.value,
        measurement=measurement,
        sub_score=spec.score(measurement),
        weight=spec.weight,
        unit=spec.unit,
    )


def _score_with(indicator_id: str, measurement: float) -> RiskScore:
    spec = INDICATOR_BY_ID[indicator_id]
    return RiskScore(
        entity_id="c1",
        entity_name="测试主体",
        score=spec.score(measurement),
        level=RiskLevel.MEDIUM,
        readings=[_reading(indicator_id, measurement)],
    )


class TestRuleAndIndicatorAgree:
    """The rule file and the indicator catalogue are one contract."""

    def test_every_rule_references_a_real_indicator(self, rules: RuleEngine) -> None:
        for rule in rules.rules:
            for clause in rule.clauses:
                indicator_id = getattr(clause, "indicator_id", None)
                if indicator_id:
                    assert indicator_id in INDICATOR_BY_ID, (
                        f"{rule.id} 引用了不存在的指标 {indicator_id}"
                    )

    def test_no_indicator_is_left_without_a_rule(self, rules: RuleEngine) -> None:
        used = {
            clause.indicator_id
            for rule in rules.rules
            for clause in rule.clauses
            if getattr(clause, "indicator_id", None)
        }
        orphans = [spec.id for spec in RISK_INDICATORS if spec.id not in used]
        assert not orphans, f"这些指标没有任何规则引用：{orphans}"

    def test_every_rule_threshold_is_reachable(self, rules: RuleEngine) -> None:
        """A rule must not demand a value the indicator can never produce.

        ``IND-ENF-001`` is a ratio in multiples of registered capital and
        tops out around 5.0. A rule demanding ``>= 50000`` — a leftover
        from the pre-1.1.0 absolute-amount semantics — can never fire, so
        the critical alert it carries is dead.
        """
        for rule in rules.rules:
            for clause in rule.clauses:
                indicator_id = getattr(clause, "indicator_id", None)
                if not indicator_id or indicator_id not in INDICATOR_BY_ID:
                    continue
                spec = INDICATOR_BY_ID[indicator_id]
                bounds = [threshold for threshold, _ in spec.thresholds]
                low, high = min(bounds), max(bounds)
                value = clause.value
                if not isinstance(value, (int, float)):
                    continue
                if clause.operator in ("gte", "gt"):
                    assert value <= high, (
                        f"{rule.id} 要求 {indicator_id} >= {value}，"
                        f"但该指标的阈值上限只有 {high} —— 规则永远不会触发"
                    )
                if clause.operator in ("lte", "lt"):
                    assert value >= low, (
                        f"{rule.id} 要求 {indicator_id} <= {value}，"
                        f"但该指标的阈值下限是 {low} —— 规则永远不会触发"
                    )

    def test_the_enforcement_rule_fires_on_a_plausible_ratio(self, rules: RuleEngine) -> None:
        """One times registered capital in enforcement is a real signal."""
        fired = [
            rule.id for rule in rules.rules if rule.matches(_score_with("IND-ENF-001", 1.0), [])
        ]
        assert "R-ENF-001" in fired, "强制执行金额达到注册资本 1 倍时未触发预警"

    def test_the_enforcement_rule_stays_quiet_below_the_threshold(self, rules: RuleEngine) -> None:
        fired = [
            rule.id for rule in rules.rules if rule.matches(_score_with("IND-ENF-001", 0.2), [])
        ]
        assert "R-ENF-001" not in fired

    def test_a_lower_is_riskier_indicator_is_not_gated_with_greater_or_equal(
        self, rules: RuleEngine
    ) -> None:
        """For ``LOWER_IS_RISKIER`` a smaller value is the risky one."""
        for rule in rules.rules:
            for clause in rule.clauses:
                indicator_id = getattr(clause, "indicator_id", None)
                if not indicator_id or indicator_id not in INDICATOR_BY_ID:
                    continue
                spec = INDICATOR_BY_ID[indicator_id]
                if spec.direction.value == "lower_is_risker":
                    assert clause.operator not in ("gte", "gt"), (
                        f"{rule.id} 对 {indicator_id} 使用了 {clause.operator}，但该指标是越低越险"
                    )


class TestCredentialsGoThroughSettings:
    """A token must be a declared, masked setting — not a raw read."""

    def test_the_tushare_token_is_read_from_the_same_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fin_kg.config.settings import Settings, reset_settings_cache

        monkeypatch.setenv("FIN_KG_TUSHARE_TOKEN", "sentinel-token-value")
        reset_settings_cache()
        try:
            settings = Settings()
            assert settings.tushare.token.get_secret_value() == "sentinel-token-value"
        finally:
            reset_settings_cache()

    def test_the_token_is_masked_in_a_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fin_kg.config.settings import Settings, reset_settings_cache

        monkeypatch.setenv("FIN_KG_TUSHARE_TOKEN", "sentinel-token-value")
        reset_settings_cache()
        try:
            assert "sentinel-token-value" not in repr(Settings().tushare)
        finally:
            reset_settings_cache()

    def test_the_adapter_keeps_the_token_out_of_its_repr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fin_kg.api.deps import get_source_adapters
        from fin_kg.config.settings import reset_settings_cache

        monkeypatch.setenv("FIN_KG_TUSHARE_TOKEN", "sentinel-token-value")
        reset_settings_cache()
        try:
            adapters = get_source_adapters()
            assert "tushare" in adapters
            assert "sentinel-token-value" not in repr(adapters["tushare"])
        finally:
            reset_settings_cache()

    def test_no_adapter_is_registered_without_a_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fin_kg.api.deps import get_source_adapters
        from fin_kg.config.settings import reset_settings_cache

        monkeypatch.delenv("FIN_KG_TUSHARE_TOKEN", raising=False)
        reset_settings_cache()
        try:
            assert "tushare" not in get_source_adapters()
        finally:
            reset_settings_cache()


class TestEverySettingIsDocumented:
    """A setting nobody can find is a setting nobody can set."""

    @staticmethod
    def _declared_in_example() -> set[str]:
        declared: set[str] = set()
        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("FIN_KG_") and "=" in line:
                declared.add(line.split("=")[0].strip())
        return declared

    @pytest.mark.parametrize(
        "key",
        [
            "FIN_KG_INGESTION_ALIAS_FILE",
            "FIN_KG_TUSHARE_TOKEN",
            "FIN_KG_GNN_LABEL_SEVERITY",
        ],
    )
    def test_the_key_is_in_the_example_file(self, key: str) -> None:
        assert key in self._declared_in_example(), f"{key} 未在 .env.example 中声明"

    def test_no_source_reads_the_environment_for_a_token_directly(self) -> None:
        """Credentials must not bypass the settings model.

        A mention of the variable name in prose is fine — what must not
        exist is an actual read of ``os.environ`` for it.
        """
        import re

        pattern = re.compile(
            r"os\.environ(?:\.get)?\(?\[?[\"']FIN_KG_(?:TUSHARE_TOKEN|LLM_API_KEY"
            r"|NEO4J_PASSWORD)[\"']"
        )
        offenders: list[str] = []
        for path in (PROJECT_ROOT / "src").rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}")
        assert not offenders, f"这些文件仍然直读密钥环境变量：{offenders}"

    def test_the_environment_variable_still_works_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing deployments keep their variable name."""
        from fin_kg.api.deps import get_source_adapters
        from fin_kg.config.settings import reset_settings_cache

        monkeypatch.setenv("FIN_KG_TUSHARE_TOKEN", "legacy-style-token")
        reset_settings_cache()
        try:
            assert "tushare" in get_source_adapters()
        finally:
            reset_settings_cache()
        assert os.environ["FIN_KG_TUSHARE_TOKEN"] == "legacy-style-token"


class TestObservabilityContracts:
    """The metrics must be usable, and their cardinality must be bounded."""

    def test_the_path_label_is_the_route_template(self) -> None:
        """A concrete path carries the entity id.

        Labelling by the concrete URL would give every entity its own time
        series, and at a hundred thousand entities the scrape becomes
        slower than the service it measures.
        """
        from fastapi.testclient import TestClient

        from fin_kg.api.app import create_app
        from fin_kg.observability.metrics import render_metrics

        client = TestClient(create_app())
        for index in range(5):
            client.get(f"/api/v1/graph/entities/entity-{index}")

        rendered = render_metrics()
        if isinstance(rendered, bytes):
            rendered = rendered.decode("utf-8")
        paths = {
            line.split('path="')[1].split('"')[0]
            for line in rendered.splitlines()
            if line.startswith("fin_kg_http_requests_total") and 'path="' in line
        }
        assert not any("entity-" in path for path in paths), (
            f"实体 ID 仍在制造独立的时序：{sorted(paths)}"
        )
        assert "/api/v1/graph/entities/{entity_id}" in paths

    def test_unmatched_urls_do_not_create_series(self) -> None:
        """A scanner probing many URLs must not create one series each."""
        from fastapi.testclient import TestClient

        from fin_kg.api.app import create_app
        from fin_kg.observability.metrics import render_metrics

        client = TestClient(create_app())
        for probe in ("/wp-admin", "/.env", "/actuator/health"):
            client.get(probe)

        rendered = render_metrics()
        if isinstance(rendered, bytes):
            rendered = rendered.decode("utf-8")
        paths = {
            line.split('path="')[1].split('"')[0]
            for line in rendered.splitlines()
            if line.startswith("fin_kg_http_requests_total") and 'path="' in line
        }
        assert not any(probe in path for path in paths for probe in ("/wp-admin", "/.env"))

    def test_the_dependency_gauge_is_written(self) -> None:
        """``dependency_up`` must have a value, or the alert never fires."""
        from fastapi.testclient import TestClient

        from fin_kg.api.app import create_app
        from fin_kg.observability.metrics import render_metrics

        client = TestClient(create_app())
        client.get("/health")

        rendered = render_metrics()
        if isinstance(rendered, bytes):
            rendered = rendered.decode("utf-8")
        values = [
            line for line in rendered.splitlines() if line.startswith("fin_kg_dependency_up{")
        ]
        assert values, "dependency_up 没有任何取值——告警规则永远不会触发"
        assert any(line.endswith("1.0") for line in values)


class TestAlertChannels:
    """Every channel the ledger claims is wired must actually be wired."""

    def test_the_file_channel_is_off_by_default(self) -> None:
        from fin_kg.config.settings import Settings
        from fin_kg.observability.alerting import AlertDispatcher

        names = [c.name for c in AlertDispatcher.from_settings(Settings()).channels]
        assert names == ["console"], "默认不应写文件——那只是一个没人要的审计轨迹"

    def test_the_file_channel_is_wired_when_configured(self) -> None:
        from fin_kg.config.settings import Settings
        from fin_kg.observability.alerting import AlertDispatcher

        settings = Settings(observability={"alert_file_path": "data/alerts.jsonl"})
        names = [c.name for c in AlertDispatcher.from_settings(settings).channels]
        assert "file" in names

    def test_the_file_channel_writes_a_line(self, tmp_path) -> None:
        import json

        from fin_kg.domain.risk import AlertSeverity, RiskAlert
        from fin_kg.observability.alerting import FileChannel

        target = tmp_path / "alerts.jsonl"
        channel = FileChannel(path=target)
        alert = RiskAlert(
            alert_id="a1",
            rule_id="R-01",
            rule_name="测试规则",
            entity_id="c1",
            entity_name="甲",
            severity=AlertSeverity.MAJOR,
            score=70.0,
            message="风险分偏高",
        )
        assert channel.send(alert) is True
        payload = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
        # The payload uses camelCase, matching the HTTP contract.
        assert payload["ruleId"] == "R-01"
        assert payload["entityId"] == "c1"


class TestRulesRejectUnknownIndicators:
    """A typo in an indicator id must fail loudly at load time."""

    def test_an_unknown_indicator_is_rejected(self, tmp_path) -> None:
        from fin_kg.core.errors import ValidationError
        from fin_kg.risk.engine import RuleEngine

        path = tmp_path / "rules.yaml"
        # Built with join rather than escapes: a backslash-newline at the
        # start of a line is rewritten by some shells on the way in.
        path.write_text(
            "\n".join(
                [
                    "rules:",
                    "  - id: R-BAD-001",
                    "    name: bad indicator",
                    "    severity: warning",
                    "    condition:",
                    "      all:",
                    "        - indicator: IND-TYPO-999",
                    "          operator: gte",
                    "          value: 1",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError) as excinfo:
            RuleEngine.from_yaml(path)
        assert "IND-TYPO-999" in str(excinfo.value.details)

    def test_the_shipped_rules_all_load(self) -> None:
        engine = RuleEngine.from_yaml(RULES_PATH)
        assert engine.rules, "规则文件解析后为空"

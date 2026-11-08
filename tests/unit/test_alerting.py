"""Unit tests for the alert dispatch channels.

The dispatcher is what turns a rule firing into a human being being told
about it, so its failure handling matters more than its happy path: a
channel that raises must not stop the other channels from receiving the
alert.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin_kg.domain.risk import AlertSeverity, RiskAlert
from fin_kg.observability.alerting import (
    AlertDispatcher,
    ConsoleChannel,
    FileChannel,
    WebhookChannel,
    alert_payload,
)


@pytest.fixture
def alert() -> RiskAlert:
    """A representative alert."""
    return RiskAlert(
        alert_id="R-CHAIN-001:company-1",
        rule_id="R-CHAIN-001",
        rule_name="担保链传导预警",
        severity=AlertSeverity.CRITICAL,
        entity_id="company-1",
        entity_name="测试公司",
        score=88.5,
        message="主体处于多层担保链中",
    )


class TestConsoleChannel:
    def test_send_succeeds(self, alert: RiskAlert) -> None:
        assert ConsoleChannel().send(alert) is True


class TestFileChannel:
    def test_send_writes_one_json_line(self, tmp_path: Path, alert: RiskAlert) -> None:
        path = tmp_path / "alerts.jsonl"
        assert FileChannel(path=path).send(alert) is True
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["alertId"] == alert.alert_id
        assert payload["severity"] == "critical"

    def test_send_appends(self, tmp_path: Path, alert: RiskAlert) -> None:
        path = tmp_path / "alerts.jsonl"
        channel = FileChannel(path=path)
        channel.send(alert)
        channel.send(alert)
        assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2

    def test_send_creates_missing_directories(self, tmp_path: Path, alert: RiskAlert) -> None:
        path = tmp_path / "nested" / "deeper" / "alerts.jsonl"
        assert FileChannel(path=path).send(alert) is True
        assert path.exists()

    def test_an_unwritable_path_returns_false(self, tmp_path: Path, alert: RiskAlert) -> None:
        # A directory cannot be opened for writing.
        path = tmp_path / "a-directory"
        path.mkdir()
        assert FileChannel(path=path).send(alert) is False


class TestWebhookChannel:
    def test_a_failed_delivery_returns_false(self, alert: RiskAlert) -> None:
        # Port 1 is not listening, so the request must fail fast.
        channel = WebhookChannel(url="http://127.0.0.1:1/hook", timeout=1.0)
        assert channel.send(alert) is False


class TestAlertPayload:
    def test_the_payload_matches_the_api_field_names(self, alert: RiskAlert) -> None:
        """The outbound payload must use the API's camelCase names.

        The API returns camelCase; if the audit file and the webhook used
        snake_case, one alert would have three shapes and a consumer would
        need three parsers.
        """
        from fin_kg.api.schemas import AlertResponse

        payload = alert_payload(alert)
        api_fields = set(AlertResponse.model_fields)
        assert api_fields <= set(payload)
        assert not any("_" in key for key in payload if key not in {"entity_id"})

    def test_the_payload_carries_the_transmission_paths(self, alert: RiskAlert) -> None:
        from fin_kg.domain.risk import TransmissionPath

        alert.transmission_paths = [
            TransmissionPath(
                node_ids=["a", "b"],
                node_names=["A", "B"],
                relation_types=["GUARANTEE"],
                transmissibilities=[1.0],
                propagated_score=42.0,
                hops=1,
            )
        ]
        payload = alert_payload(alert)
        assert payload["transmissionPaths"][0]["nodeIds"] == ["a", "b"]
        assert payload["transmissionPaths"][0]["text"]

    def test_the_payload_is_json_serialisable(self, alert: RiskAlert) -> None:
        json.dumps(alert_payload(alert), ensure_ascii=False)


class TestAlertDispatcher:
    def test_every_channel_is_tried(self, tmp_path: Path, alert: RiskAlert) -> None:
        path = tmp_path / "alerts.jsonl"
        dispatcher = AlertDispatcher(channels=[ConsoleChannel(), FileChannel(path=path)])
        results = dispatcher.dispatch(alert)
        assert results["console"] is True
        assert results["file"] is True
        assert path.exists()

    def test_one_failing_channel_does_not_block_the_others(
        self, tmp_path: Path, alert: RiskAlert
    ) -> None:
        path = tmp_path / "alerts.jsonl"
        dispatcher = AlertDispatcher(
            channels=[
                WebhookChannel(url="http://127.0.0.1:1/hook", timeout=1.0),
                FileChannel(path=path),
            ]
        )
        results = dispatcher.dispatch(alert)
        assert results["webhook"] is False
        assert results["file"] is True
        assert path.exists()

    def test_a_raising_channel_is_contained(self, alert: RiskAlert) -> None:
        class Exploding:
            name = "boom"

            def send(self, _alert: RiskAlert) -> bool:
                raise RuntimeError("channel exploded")

        dispatcher = AlertDispatcher(channels=[Exploding(), ConsoleChannel()])
        results = dispatcher.dispatch(alert)
        assert results["boom"] is False
        assert results["console"] is True

    def test_dispatch_many_counts_the_outcomes(self, tmp_path: Path) -> None:
        path = tmp_path / "alerts.jsonl"
        dispatcher = AlertDispatcher(channels=[FileChannel(path=path)])
        alerts = [
            RiskAlert(
                alert_id=f"a{index}",
                rule_id="R",
                rule_name="r",
                severity=AlertSeverity.WARNING,
                entity_id="c",
                message="m",
            )
            for index in range(3)
        ]
        counts = dispatcher.dispatch_many(alerts)
        assert counts == {"sent": 3, "failed": 0}

    def test_dispatch_many_counts_a_total_failure(self) -> None:
        class AlwaysFails:
            name = "fail"

            def send(self, _alert: RiskAlert) -> bool:
                return False

        dispatcher = AlertDispatcher(channels=[AlwaysFails()])
        counts = dispatcher.dispatch_many(
            [
                RiskAlert(
                    alert_id="a",
                    rule_id="R",
                    rule_name="r",
                    severity=AlertSeverity.WARNING,
                    entity_id="c",
                    message="m",
                )
            ]
        )
        assert counts == {"sent": 0, "failed": 1}

    def test_from_settings_adds_the_console_channel(self) -> None:
        from fin_kg.config.settings import Settings

        settings = Settings(observability={"alert_webhook_url": ""})
        dispatcher = AlertDispatcher.from_settings(settings)
        assert [channel.name for channel in dispatcher.channels] == ["console"]

    def test_from_settings_adds_the_webhook_channel(self) -> None:
        from fin_kg.config.settings import Settings

        settings = Settings(observability={"alert_webhook_url": "http://example.test/hook"})
        dispatcher = AlertDispatcher.from_settings(settings)
        assert {channel.name for channel in dispatcher.channels} == {"console", "webhook"}

"""Alert dispatch.

When a risk alert fires, it has to reach a human. This module routes it to
the configured channels. It is deliberately small: the platform does not
replace the alerting system an institution already runs, it feeds it.

Channels:

``webhook``   POST the alert payload to a configured HTTP endpoint
``console``   log the alert (always on, and the fallback when no webhook
              is configured)
``file``      append the alert to a JSON Lines file, for audit and replay
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fin_kg.core.logging import get_logger
from fin_kg.domain.risk import RiskAlert

logger = get_logger(__name__)


class AlertChannel(Protocol):
    """The interface every alert channel implements."""

    name: str

    def send(self, alert: RiskAlert) -> bool:
        """Deliver one alert. Returns ``True`` on success."""


def alert_payload(alert: RiskAlert) -> dict[str, Any]:
    """Serialise an alert for an outbound channel.

    The keys are camelCase, matching the API contract in
    ``fin_kg.api.schemas.AlertResponse``. Without this, the same alert would
    have three different shapes depending on where it went: camelCase over
    HTTP, snake_case in the audit file, and snake_case in the webhook body.
    A consumer would need three parsers for one alert.
    """
    payload: dict[str, Any] = {
        "alertId": alert.alert_id,
        "ruleId": alert.rule_id,
        "ruleName": alert.rule_name,
        "severity": alert.severity.value,
        "status": alert.status.value,
        "entityId": alert.entity_id,
        "entityName": alert.entity_name,
        "entityType": alert.entity_type,
        "score": alert.score,
        "message": alert.message,
        "evidence": alert.evidence,
        "transmissionPaths": [
            {
                "nodeIds": list(path.node_ids),
                "nodeNames": list(path.node_names),
                "relationTypes": list(path.relation_types),
                "hops": path.hops,
                "propagatedScore": path.propagated_score,
                "text": path.to_text(),
            }
            for path in alert.transmission_paths
        ],
        "affectedEntityIds": list(alert.affected_entity_ids),
        "triggeredAt": alert.triggered_at.isoformat(),
        "modelVersion": alert.model_version,
        "fingerprint": alert.fingerprint,
    }
    return payload


@dataclass(slots=True)
class ConsoleChannel:
    """Logs the alert. Always available."""

    name: str = "console"

    def send(self, alert: RiskAlert) -> bool:
        logger.warning(
            "RISK ALERT [%s] %s | %s | 分值 %.1f | %s",
            alert.severity.value.upper(),
            alert.rule_id,
            alert.entity_name or alert.entity_id,
            alert.score,
            alert.message,
        )
        return True


@dataclass(slots=True)
class WebhookChannel:
    """POSTs the alert to an HTTP endpoint."""

    url: str
    timeout: float = 10.0
    name: str = "webhook"

    def send(self, alert: RiskAlert) -> bool:
        import httpx

        payload = alert_payload(alert)
        try:
            response = httpx.post(self.url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            return True
        except Exception as exc:  # a failed delivery must not raise
            logger.error("webhook delivery failed for %s: %s", alert.alert_id, exc)
            return False


@dataclass(slots=True)
class FileChannel:
    """Appends the alert to a JSON Lines file."""

    path: Path
    name: str = "file"

    def send(self, alert: RiskAlert) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(alert_payload(alert), ensure_ascii=False))
                handle.write("\n")
            return True
        except OSError as exc:
            logger.error("file delivery failed for %s: %s", alert.alert_id, exc)
            return False


@dataclass(slots=True)
class AlertDispatcher:
    """Fans an alert out to every configured channel."""

    channels: list[AlertChannel]

    @classmethod
    def from_settings(cls, settings: Any) -> AlertDispatcher:
        """Build the dispatcher from the application settings."""
        channels: list[AlertChannel] = [ConsoleChannel()]
        webhook = settings.observability.alert_webhook_url
        if webhook:
            channels.append(WebhookChannel(url=webhook))
        return cls(channels=channels)

    def dispatch(self, alert: RiskAlert) -> dict[str, bool]:
        """Send one alert to every channel."""
        results: dict[str, bool] = {}
        for channel in self.channels:
            try:
                results[channel.name] = channel.send(alert)
            except Exception as exc:  # one bad channel must not block others
                logger.error("channel %s raised on %s: %s", channel.name, alert.alert_id, exc)
                results[channel.name] = False
        return results

    def dispatch_many(self, alerts: list[RiskAlert]) -> dict[str, int]:
        """Send several alerts and count the successes."""
        counts = {"sent": 0, "failed": 0}
        for alert in alerts:
            results = self.dispatch(alert)
            if any(results.values()):
                counts["sent"] += 1
            else:
                counts["failed"] += 1
        return counts


__all__ = [
    "AlertChannel",
    "AlertDispatcher",
    "ConsoleChannel",
    "FileChannel",
    "WebhookChannel",
    "alert_payload",
]

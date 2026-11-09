"""Rule engine.

A *rule* turns graph state into an alert. Rules are declarative and live in
``rules/risk_rules.yaml`` so that a risk analyst can add or tune a rule
without touching Python.

Rule shape::

    id: R-CHAIN-001
    name: 担保链传导预警
    severity: major
    description: ...
    enabled: true
    scope:
      entity_types: [company]
    condition:
      all:
        - indicator: IND-GUA-001
          operator: gte
          value: 50000
        - path:
            relation_types: [GUARANTEE]
            min_hops: 2
            min_paths: 1
    message: 主体对外担保余额过高，且已形成 {path_count} 条担保链
    evidence:
      indicators: [IND-GUA-001]
      show_paths: true

Supported operators: ``gte``, ``gt``, ``lte``, ``lt``, ``eq``, ``neq``,
``between``, ``in``.

A rule fires when *every* clause in ``condition.all`` holds (AND semantics).
Rules are evaluated in file order and all matching rules fire — a rule engine
that stops at the first match would hide the full picture from an analyst.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from fin_kg.core.errors import RuleEvaluationError, ValidationError
from fin_kg.core.logging import get_logger
from fin_kg.domain.indicators import INDICATOR_BY_ID
from fin_kg.domain.relations import RelationType
from fin_kg.domain.risk import (
    AlertSeverity,
    IndicatorReading,
    RiskAlert,
    RiskAssessment,
    RiskScore,
    TransmissionPath,
)
from fin_kg.observability.metrics import ALERTS

logger = get_logger(__name__)

#: Operators understood by the rule engine.
OPERATORS: tuple[str, ...] = (
    "gte",
    "gt",
    "lte",
    "lt",
    "eq",
    "neq",
    "between",
    "in",
)


@dataclass(frozen=True, slots=True)
class Clause:
    """One condition clause of a rule."""

    kind: str  # "indicator" | "score" | "path"
    indicator_id: str | None = None
    operator: str | None = None
    value: Any = None
    value_high: Any = None
    relation_types: tuple[RelationType, ...] = ()
    min_hops: int = 1
    min_paths: int = 1

    def evaluate(
        self,
        score: RiskScore,
        paths: list[TransmissionPath],
    ) -> bool:
        """Evaluate this clause against a scored entity."""
        if self.kind == "score":
            return _compare(score.score, self.operator or "gte", self.value, self.value_high)
        if self.kind == "indicator":
            reading = _find_reading(score.readings, self.indicator_id or "")
            if reading is None or reading.measurement is None:
                return False
            return _compare(
                reading.measurement, self.operator or "gte", self.value, self.value_high
            )
        if self.kind == "path":
            matching = [
                path
                for path in paths
                if path.hops >= self.min_hops
                and (
                    not self.relation_types
                    or all(relation in self.relation_types for relation in path.relation_types)
                )
            ]
            return len(matching) >= self.min_paths
        raise RuleEvaluationError(f"unknown clause kind: {self.kind}")


@dataclass(frozen=True, slots=True)
class Rule:
    """A declarative alert rule."""

    id: str
    name: str
    severity: AlertSeverity
    description: str
    clauses: tuple[Clause, ...]
    message: str
    entity_types: tuple[str, ...] = ("company",)
    enabled: bool = True
    show_paths: bool = True
    score_threshold: float | None = None

    def matches(self, score: RiskScore, paths: list[TransmissionPath]) -> bool:
        """Whether this rule fires for the given scored entity."""
        if not self.enabled:
            return False
        if self.entity_types and score.entity_type not in self.entity_types:
            return False
        if self.score_threshold is not None and score.score < self.score_threshold:
            return False
        return all(clause.evaluate(score, paths) for clause in self.clauses)

    def render_message(self, score: RiskScore, paths: list[TransmissionPath]) -> str:
        """Fill the message template with the live values."""
        return self.message.format(
            entity=score.entity_name or score.entity_id,
            score=score.score,
            level=score.level.value,
            path_count=len(paths),
            path_summary="; ".join(path.to_text() for path in paths[:3]) or "无",
            top_indicator=_describe_reading(score),
        )


def _find_reading(readings: list[IndicatorReading], indicator_id: str) -> IndicatorReading | None:
    for reading in readings:
        if reading.indicator_id == indicator_id:
            return reading
    return None


def _describe_reading(score: RiskScore) -> str:
    """Render the dominant contributor as ``指标名 数值 单位``.

    The raw measurement is included, not just the indicator name: a message
    that says "涉诉事件数" without a number tells an analyst nothing.
    """
    contributors = score.top_contributors
    if not contributors:
        return "无"
    top = contributors[0]
    if top.measurement is None:
        return f"{top.indicator_name}无数据"
    return f"{top.indicator_name} {top.measurement:g}{top.unit}"


def _compare(actual: Any, operator: str, value: Any, value_high: Any = None) -> bool:
    """Apply one comparison operator."""
    if operator not in OPERATORS:
        raise RuleEvaluationError(f"unsupported operator: {operator}")
    if actual is None:
        return False
    try:
        if operator == "gte":
            return float(actual) >= float(value)
        if operator == "gt":
            return float(actual) > float(value)
        if operator == "lte":
            return float(actual) <= float(value)
        if operator == "lt":
            return float(actual) < float(value)
        if operator == "eq":
            return float(actual) == float(value)
        if operator == "neq":
            return float(actual) != float(value)
        if operator == "between":
            if value_high is None:
                raise RuleEvaluationError("'between' requires an upper bound")
            return float(value) <= float(actual) <= float(value_high)
        if operator == "in":
            return float(actual) in {float(item) for item in value}
    except (TypeError, ValueError) as exc:
        raise RuleEvaluationError(
            "rule comparison failed",
            details={"operator": operator, "actual": actual, "value": value},
        ) from exc
    return False


@dataclass(slots=True)
class RuleEngine:
    """Loads rules and evaluates them against scored entities."""

    rules: list[Rule] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> RuleEngine:
        """Load rules from a YAML file."""
        if not Path(path).exists():
            raise ValidationError("risk rule file not found", details={"path": str(path)})
        try:
            payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValidationError(
                "risk rule file is not valid YAML", details={"path": str(path), "error": str(exc)}
            ) from exc
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RuleEngine:
        """Build an engine from an already-parsed rule document."""
        if not isinstance(payload, dict):
            raise ValidationError("rule document must be a mapping")
        raw_rules = payload.get("rules") or []
        if not isinstance(raw_rules, list):
            raise ValidationError("'rules' must be a list")
        rules = [cls._parse_rule(item) for item in raw_rules]
        logger.info("loaded %s risk rules", len(rules))
        return cls(rules=rules)

    @staticmethod
    def _parse_rule(raw: dict[str, Any]) -> Rule:
        rule_id = str(raw.get("id", "")).strip()
        if not rule_id:
            raise ValidationError("every rule needs an id")
        severity = raw.get("severity", "warning")
        try:
            severity_enum = AlertSeverity(str(severity).lower())
        except ValueError as exc:
            raise ValidationError(
                f"rule {rule_id} has an invalid severity", details={"severity": severity}
            ) from exc

        condition = raw.get("condition") or {}
        clauses: list[Clause] = []
        for entry in condition.get("all", []) or []:
            clauses.append(RuleEngine._parse_clause(rule_id, entry))

        if not clauses:
            raise ValidationError(f"rule {rule_id} has no condition clauses")

        scope = raw.get("scope") or {}
        entity_types = tuple(scope.get("entity_types") or ("company",))
        evidence = raw.get("evidence") or {}

        return Rule(
            id=rule_id,
            name=str(raw.get("name", rule_id)),
            severity=severity_enum,
            description=str(raw.get("description", "")),
            clauses=tuple(clauses),
            message=str(raw.get("message", f"{rule_id} triggered")),
            entity_types=entity_types,
            enabled=bool(raw.get("enabled", True)),
            show_paths=bool(evidence.get("show_paths", True)),
            score_threshold=raw.get("score_threshold"),
        )

    @staticmethod
    def _parse_clause(rule_id: str, entry: dict[str, Any]) -> Clause:
        if "indicator" in entry:
            operator = str(entry.get("operator", "gte"))
            if operator not in OPERATORS:
                raise ValidationError(
                    f"rule {rule_id} uses an unsupported operator", details={"operator": operator}
                )
            indicator_id = str(entry["indicator"])
            # A typo in the indicator id would otherwise load cleanly and
            # then never fire, with nothing in the logs to explain why.
            if indicator_id not in INDICATOR_BY_ID:
                raise ValidationError(
                    f"rule {rule_id} references an unknown indicator",
                    details={"indicator": indicator_id},
                )
            return Clause(
                kind="indicator",
                indicator_id=indicator_id,
                operator=operator,
                value=entry.get("value"),
                value_high=entry.get("value_high"),
            )
        if "score" in entry:
            spec = entry["score"] or {}
            operator = str(spec.get("operator", "gte"))
            if operator not in OPERATORS:
                raise ValidationError(
                    f"rule {rule_id} uses an unsupported operator", details={"operator": operator}
                )
            return Clause(
                kind="score",
                operator=operator,
                value=spec.get("value"),
                value_high=spec.get("value_high"),
            )
        if "path" in entry:
            path = entry["path"] or {}
            relation_types = tuple(
                RelationType(str(item).upper()) for item in path.get("relation_types", [])
            )
            return Clause(
                kind="path",
                relation_types=relation_types,
                min_hops=int(path.get("min_hops", 1)),
                min_paths=int(path.get("min_paths", 1)),
            )
        raise ValidationError(f"rule {rule_id} has an unrecognised condition clause")

    def evaluate(
        self,
        score: RiskScore,
        paths: list[TransmissionPath] | None = None,
        *,
        fingerprint_of: Callable[[RiskAlert], str] | None = None,
    ) -> list[RiskAlert]:
        """Evaluate every rule against one scored entity."""
        path_list = paths or []
        alerts: list[RiskAlert] = []
        for rule in self.rules:
            try:
                if not rule.matches(score, path_list):
                    continue
            except RuleEvaluationError as exc:
                logger.warning("rule %s evaluation failed: %s", rule.id, exc.message)
                continue

            alert = RiskAlert(
                alert_id=f"{rule.id}:{score.entity_id}",
                rule_id=rule.id,
                rule_name=rule.name,
                severity=rule.severity,
                entity_id=score.entity_id,
                entity_name=score.entity_name,
                entity_type=score.entity_type,
                score=score.score,
                message=rule.render_message(score, path_list),
                evidence={
                    "indicators": [
                        {
                            "id": reading.indicator_id,
                            "name": reading.indicator_name,
                            "measurement": reading.measurement,
                            "subScore": reading.sub_score,
                            "weight": reading.weight,
                            "version": reading.version,
                        }
                        for reading in score.top_contributors
                    ],
                    "level": score.level.value,
                    "modelVersion": score.model_version,
                },
                transmission_paths=path_list[:5] if rule.show_paths else [],
                affected_entity_ids=[node for path in path_list for node in path.node_ids[1:]],
            )
            if fingerprint_of is not None:
                alert.fingerprint = fingerprint_of(alert)
            alerts.append(alert)
            ALERTS.labels(rule=rule.id, severity=alert.severity.value).inc()
        return alerts

    def evaluate_assessment(
        self,
        score: RiskScore,
        paths: list[TransmissionPath] | None = None,
    ) -> RiskAssessment:
        """Evaluate the rules and package the result with the paths."""
        path_list = paths or []
        alerts = self.evaluate(score, path_list)
        affected = [
            {
                "id": node_id,
                "name": node_name,
                "hops": hops,
            }
            for path in path_list
            for node_id, node_name, hops in zip(
                path.node_ids[1:], path.node_names[1:], range(1, len(path.node_ids)), strict=False
            )
        ]
        return RiskAssessment(
            score=score,
            alerts=alerts,
            transmission_paths=path_list,
            affected_entities=affected,
        )


def alert_fingerprint(alert: RiskAlert) -> str:
    """A stable identity for an alert, used for deduplication across runs.

    The fingerprint is built from the alert's *identity* — the rule that
    fired and the entity it fired on — and not from its rendered message.
    The message embeds the current score and the path summary, both of
    which change between runs; including them would give the same alert a
    new fingerprint every time and defeat the deduplication.
    """
    import hashlib

    material = f"{alert.rule_id}|{alert.entity_id}".encode()
    return hashlib.sha256(material).hexdigest()[:24]


__all__ = [
    "OPERATORS",
    "Clause",
    "Rule",
    "RuleEngine",
    "alert_fingerprint",
]

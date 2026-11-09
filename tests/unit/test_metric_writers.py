"""Every metric the platform exports must actually be written.

A metric that is declared but never written is worse than no metric at
all: it shows up in ``/metrics`` with a zero value, an alert rule that
references it looks like it is working, and the thing it was supposed to
watch is silently unwatched.

The check is a *static* scan of the source tree, not a render of the
registry. A counter without labels renders as a zero sample even when it
is never incremented, and a labelled histogram does not render at all
until it is observed — so the registry cannot tell you whether anything
writes them.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "src"
METRICS_MODULE = SOURCE_DIR / "fin_kg" / "observability" / "metrics.py"


def _declared_metrics() -> list[str]:
    """The names of the metric objects the module exports."""
    tree = ast.parse(METRICS_MODULE.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                names.append(target.id)
    # REGISTRY is the Prometheus registry itself, not a metric.
    return [name for name in names if name != "REGISTRY"]


def _writer_sites() -> dict[str, list[str]]:
    """Where each metric name is used in a writing position.

    A metric is written when it is followed by ``.inc()``, ``.set()``,
    ``.observe()`` or ``.labels(...)`` — the last one because a labelled
    metric is always chained into one of the first three.
    """
    sites: dict[str, list[str]] = {}
    for path in SOURCE_DIR.rglob("*.py"):
        if path == METRICS_MODULE:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"\b([A-Z][A-Z0-9_]{2,})\b[^\n]*?\.(?:inc|set|observe|labels)\(", text
        ):
            name = match.group(1)
            line = text[: match.start()].count("\n") + 1
            sites.setdefault(name, []).append(f"{path.name}:{line}")
    return sites


@pytest.mark.parametrize("metric", _declared_metrics())
def test_every_declared_metric_has_a_writer(metric: str) -> None:
    sites = _writer_sites()
    assert sites.get(metric), (
        f"{metric} 被声明并导出，但代码里没有任何地方写入它——"
        f"它在 /metrics 里恒为 0，引用它的告警规则永远拿不到数据"
    )


def test_the_documented_alert_rules_reference_metrics_that_are_written() -> None:
    """The alert rules in the deployment guide must be actionable.

    Each rule names a metric. If that metric is never written, the rule
    can never fire, and the operator believes the platform is watching
    something it is not.
    """
    deployment = (PROJECT_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"fin_kg_[a-z0-9_]+", deployment))
    assert referenced, "部署文档里没有引用任何指标——告警规则可能已被删掉"

    sites = _writer_sites()
    declared = set(_declared_metrics())
    unwritten = []
    for metric_name in sorted(referenced):
        # Map the exported name onto the metric family it produces.
        candidates = [
            name
            for name in declared
            if metric_name.startswith(name.lower().replace("_", "_")) or name.lower() in metric_name
        ]
        if not candidates:
            continue
        if not any(sites.get(name) for name in candidates):
            unwritten.append((metric_name, candidates))
    assert not unwritten, f"文档告警规则引用了从不被写入的指标：{unwritten}"


def test_the_metric_module_does_not_write_itself() -> None:
    """The definitions live in one place; the writes live in another.

    A metric that increments itself inside its own definition would make
    the static scan above meaningless.
    """
    text = METRICS_MODULE.read_text(encoding="utf-8")
    for match in re.finditer(
        r"\b([A-Z][A-Z0-9_]{2,})\b[^\n]*?\.(?:inc|set|observe|labels)\(", text
    ):
        pytest.fail(f"metrics.py 不应写入自己的指标：{match.group(1)}")

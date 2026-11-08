"""性能基准测试。

测量四个关键路径的耗时，用于容量规划与回归对比：

1. **摄入吞吐** —— 节点/关系写入速度
2. **k 跳遍历** —— 路径检索延迟
3. **风险评分** —— 单主体评分耗时
4. **风险传导** —— 传导计算耗时

用法::

    python scripts/benchmark.py --companies 10000 --hops 3
    python scripts/benchmark.py --compare          # 与基线对比
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fin_kg.graph.repository import InMemoryGraphRepository
from fin_kg.ingestion.pipeline import IngestionPipeline
from fin_kg.ingestion.sources import SyntheticSourceAdapter
from fin_kg.risk.engine import RuleEngine
from fin_kg.risk.service import build_service

#: The recorded baseline, used by ``--compare``.
BASELINE: dict[str, Any] = {
    "companies": 10_000,
    "ingestSeconds": 12.0,
    "traverseP95Ms": 45.0,
    "scoreP95Ms": 8.0,
    "propagateP95Ms": 30.0,
}


@dataclass
class Measurement:
    """A timing sample set."""

    name: str
    samples: list[float] = field(default_factory=list)
    unit: str = "ms"

    @property
    def p50(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def p95(self) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[index]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "samples": len(self.samples),
            "p50": round(self.p50, 3),
            "p95": round(self.p95, 3),
            "min": round(min(self.samples), 3) if self.samples else 0.0,
            "max": round(max(self.samples), 3) if self.samples else 0.0,
        }


def _time_call(function: Any, *args: Any, **kwargs: Any) -> float:
    """Run a callable and return the elapsed milliseconds."""
    started = time.perf_counter()
    function(*args, **kwargs)
    return (time.perf_counter() - started) * 1000


def run_benchmark(*, companies: int, hops: int, samples: int, rules_path: Path) -> dict[str, Any]:
    """Run the benchmark and return the report."""
    repository = InMemoryGraphRepository()
    repository.ensure_schema()

    print(f"[1/4] 摄入 {companies} 家主体 ...")
    pipeline = IngestionPipeline(repository)
    source = SyntheticSourceAdapter(companies=companies, events_per_company=1.5)
    started = time.perf_counter()
    report = pipeline.run([source])
    ingest_seconds = time.perf_counter() - started
    print(
        f"      节点 {report.nodes_written}，关系 {report.relations_written}，"
        f"耗时 {ingest_seconds:.2f}s"
    )

    rules = RuleEngine.from_yaml(rules_path)
    service = build_service(repository, rules)

    entity_ids = [node["id"] for node in repository.find_nodes("Company")]

    print(f"[2/4] {hops} 跳遍历 {samples} 次 ...")
    traverse = Measurement("traverse")
    for index in range(samples):
        entity_id = entity_ids[index % len(entity_ids)]
        traverse.samples.append(
            _time_call(
                repository.traverse,
                entity_id,
                list(__import__("fin_kg.domain.relations", fromlist=["RelationType"]).RelationType),
                max_hops=hops,
                direction="out",
            )
        )

    print(f"[3/4] 风险评分 {samples} 次 ...")
    score = Measurement("score")
    for index in range(samples):
        entity_id = entity_ids[index % len(entity_ids)]
        score.samples.append(_time_call(service.score, entity_id))

    print(f"[4/4] 风险传导 {samples} 次 ...")
    propagate = Measurement("propagate")
    for index in range(samples):
        entity_id = entity_ids[index % len(entity_ids)]
        risk_score = service.score(entity_id)
        propagate.samples.append(_time_call(service.propagate, risk_score))

    return {
        "config": {
            "companies": companies,
            "hops": hops,
            "samples": samples,
        },
        "ingest": {
            "seconds": round(ingest_seconds, 3),
            "nodes": report.nodes_written,
            "relations": report.relations_written,
            "nodesPerSecond": round(report.nodes_written / max(ingest_seconds, 0.001), 1),
        },
        "measurements": [
            traverse.as_dict(),
            score.as_dict(),
            propagate.as_dict(),
        ],
    }


def compare(result: dict[str, Any]) -> dict[str, Any]:
    """Compare the result against the recorded baseline."""
    measurements = {item["name"]: item for item in result["measurements"]}
    current = {
        "companies": result["config"]["companies"],
        "ingestSeconds": result["ingest"]["seconds"],
        "traverseP95Ms": measurements["traverse"]["p95"],
        "scoreP95Ms": measurements["score"]["p95"],
        "propagateP95Ms": measurements["propagate"]["p95"],
    }
    deltas: dict[str, Any] = {}
    for key, base in BASELINE.items():
        if key == "companies":
            continue
        actual = current[key]
        ratio = actual / base if base else 0.0
        deltas[key] = {
            "baseline": base,
            "current": round(actual, 3),
            "ratio": round(ratio, 3),
            "status": "regressed" if ratio > 1.5 else ("improved" if ratio < 0.8 else "ok"),
        }
    return deltas


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="性能基准测试")
    parser.add_argument("--companies", type=int, default=2_000)
    parser.add_argument("--hops", type=int, default=3)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--rules", default="rules/risk_rules.yaml")
    parser.add_argument("--compare", action="store_true", help="与基线对比")
    parser.add_argument("--output", default=None, help="结果写入 JSON 文件")
    args = parser.parse_args()

    result = run_benchmark(
        companies=args.companies,
        hops=args.hops,
        samples=args.samples,
        rules_path=Path(args.rules),
    )

    print()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.compare:
        print()
        print("=== 与基线对比 ===")
        print(json.dumps(compare(result), ensure_ascii=False, indent=2))

    if args.output:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nwritten to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

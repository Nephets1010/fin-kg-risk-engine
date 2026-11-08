"""Command line interface.

``fin-kg <command>`` is the operator entry point. Every command is
non-interactive and returns a non-zero exit code on failure, so it can be
used in a Makefile, a CI job or a cron entry.

Commands
--------
``migrate``    apply the graph schema
``ingest``     run an ingestion over the selected source
``seed``       load the synthetic dataset into the graph
``score``      recompute the risk scores
``report``     print a research briefing
``ask``        ask a question
``schema``     print the schema description
``rules``      list the loaded rules
``serve``      run the API
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from fin_kg import __version__
from fin_kg.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _settings() -> Any:
    from fin_kg.config.settings import get_settings

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return settings


def _repository() -> Any:
    """Build the graph repository. The CLI talks to the store directly."""
    from fin_kg.api.deps import create_repository

    return create_repository(_settings())


def _rules() -> Any:
    """Load the risk rules from the configured rule file."""
    from fin_kg.risk.engine import RuleEngine

    settings = _settings()
    return RuleEngine.from_yaml(settings.resolve_path(settings.risk.rules_path))


def _risk_service(repository: Any) -> Any:
    """Build the risk service. The CLI does not use FastAPI's DI."""
    from fin_kg.risk.service import RiskService, RiskServiceConfig

    settings = _settings()
    return RiskService(
        repository=repository,
        rules=_rules(),
        indicator_engine=_build_indicator_engine(repository),
        config=RiskServiceConfig(
            alert_threshold=settings.risk.alert_threshold,
            critical_threshold=settings.risk.critical_threshold,
            max_hops=settings.risk.max_hops,
            decay=settings.risk.decay,
        ),
    )


def _build_indicator_engine(repository: Any) -> Any:
    from fin_kg.graph.repository import Neo4jGraphRepository
    from fin_kg.risk.indicators_engine import GraphIndicatorEngine, LocalIndicatorEngine

    if isinstance(repository, Neo4jGraphRepository):
        return GraphIndicatorEngine(repository)
    return LocalIndicatorEngine(repository)


def cmd_migrate(args: argparse.Namespace) -> int:
    """Apply the graph schema."""
    from fin_kg.graph.schema import LATEST_SCHEMA_VERSION

    repository = _repository()
    repository.ensure_schema()
    print(f"schema applied, target version {LATEST_SCHEMA_VERSION}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Run an ingestion over the selected source."""
    from datetime import date

    from fin_kg.api.deps import get_source_adapters
    from fin_kg.ingestion.pipeline import IngestionPipeline

    settings = _settings()
    adapters = get_source_adapters()
    source = adapters.get(args.source)
    if source is None:
        print(f"unknown source '{args.source}'; available: {sorted(adapters)}", file=sys.stderr)
        return 2

    pipeline = IngestionPipeline(
        _repository(),
        batch_size=settings.ingestion.batch_size,
        checkpoint_dir=settings.resolve_path(settings.ingestion.checkpoint_dir),
    )
    since = date.fromisoformat(args.since) if args.since else None
    report = pipeline.run([source], since=since, limit=args.limit)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """Load the synthetic dataset into the graph."""
    from fin_kg.ingestion.pipeline import IngestionPipeline
    from fin_kg.ingestion.sources import SyntheticSourceAdapter

    settings = _settings()
    adapter = SyntheticSourceAdapter(
        companies=args.companies, events_per_company=args.events, seed=args.seed
    )
    pipeline = IngestionPipeline(_repository(), batch_size=settings.ingestion.batch_size)
    report = pipeline.run([adapter])
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Recompute the risk scores of the graph."""
    service = _risk_service(_repository())
    scores = service.rank(limit=args.limit)
    for score in scores:
        print(f"{score.score:6.2f}  {score.level.value:<8} {score.entity_name or score.entity_id}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Print a research briefing for one entity."""
    from fin_kg.research.report import ReportGenerator

    repository = _repository()
    service = _risk_service(repository)
    generator = ReportGenerator(repository)
    assessment = service.assess(args.entity_id)
    report = generator.generate(assessment)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.to_markdown())
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Ask a question about the graph."""
    from fin_kg.research.kgqa import KGQA

    repository = _repository()
    # Refresh the stored risk scores so the answer reflects the current graph.
    _risk_service(repository).rank(limit=len(repository.find_nodes("Company")))
    qa = KGQA(repository)
    answer = qa.ask(args.question)
    if args.json:
        print(json.dumps(answer.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(answer.answer)
        print(f"\n[意图] {answer.intent.value}  [置信度] {answer.confidence:.2f}")
        for item in answer.evidence:
            print(f"  · {item.description}")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    """Print the graph schema description."""
    from fin_kg.graph.schema import describe_schema

    print(describe_schema())
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    """List the loaded risk rules."""
    engine = _rules()
    for rule in engine.rules:
        state = "enabled" if rule.enabled else "disabled"
        print(f"{rule.id:<14} {rule.severity.value:<9} {state:<9} {rule.name}")
    print(f"\n共 {len(engine.rules)} 条规则")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the API server."""
    import uvicorn

    settings = _settings()
    uvicorn.run(
        "fin_kg.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        workers=settings.workers if not args.reload else 1,
        reload=args.reload,
        log_config=None,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="fin-kg",
        description="金融知识图谱风险预警与智能投研系统",
    )
    parser.add_argument("--version", action="version", version=f"fin-kg {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="应用图谱 schema")
    migrate.set_defaults(func=cmd_migrate)

    ingest = subparsers.add_parser("ingest", help="执行数据接入")
    ingest.add_argument("--source", default="synthetic", help="数据源名称")
    ingest.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD")
    ingest.add_argument("--limit", type=int, default=None, help="最大记录数")
    ingest.set_defaults(func=cmd_ingest)

    seed = subparsers.add_parser("seed", help="载入合成数据")
    seed.add_argument("--companies", type=int, default=100)
    seed.add_argument("--events", type=float, default=1.5)
    seed.add_argument("--seed", type=int, default=20240101)
    seed.set_defaults(func=cmd_seed)

    score = subparsers.add_parser("score", help="重算风险分")
    score.add_argument("--limit", type=int, default=20)
    score.set_defaults(func=cmd_score)

    report = subparsers.add_parser("report", help="生成投研简报")
    report.add_argument("entity_id")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    ask = subparsers.add_parser("ask", help="图谱问答")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true")
    ask.set_defaults(func=cmd_ask)

    schema = subparsers.add_parser("schema", help="打印图谱 schema")
    schema.set_defaults(func=cmd_schema)

    rules = subparsers.add_parser("rules", help="列出风险规则")
    rules.set_defaults(func=cmd_rules)

    serve = subparsers.add_parser("serve", help="启动 API 服务")
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``fin-kg`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # the CLI reports errors as exit codes
        print(f"error: {exc}", file=sys.stderr)
        logger.exception("command %s failed", args.command)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

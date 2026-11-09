"""Graph schema: constraints, indexes and the Cypher migration set.

The schema is versioned. :data:`MIGRATIONS` is an ordered list of
``(version, description, statements)`` triples; the store applies them once
and records the applied version in a ``SchemaMigration`` node. Re-running the
schema application is therefore idempotent, which matters because the
ingestion pipeline and the API both ensure the schema on startup.

Index strategy
--------------
* **Unique constraints** on ``Company.id``, ``Person.id``, ``Event.id``,
  ``Product.id`` and ``Company.creditCode`` — the last one is what makes
  entity alignment against the registry reliable.
* **Range indexes** on ``HOLD.percent`` and ``Event.occurredDate`` — used by
  the propagation and indicator queries respectively.
* **Full-text / property indexes** on ``Company.name`` and ``Person.name``
  for fuzzy lookups in the KGQA module.
"""

from __future__ import annotations

from dataclasses import dataclass

from fin_kg.domain.indicators import RISK_INDICATORS
from fin_kg.domain.relations import RELATION_SCHEMA, RelationType


@dataclass(frozen=True, slots=True)
class Migration:
    """One versioned schema change."""

    version: int
    description: str
    statements: tuple[str, ...]


#: Ordered, idempotent schema migrations.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="节点唯一约束与标签",
        statements=(
            "CREATE CONSTRAINT company_id IF NOT EXISTS FOR (n:Company) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT person_id IF NOT EXISTS FOR (n:Person) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT event_id IF NOT EXISTS FOR (n:Event) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT product_id IF NOT EXISTS FOR (n:Product) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
        ),
    ),
    Migration(
        version=2,
        description="工商信用代码唯一约束（实体对齐锚点）",
        statements=(
            "CREATE CONSTRAINT company_credit_code IF NOT EXISTS "
            "FOR (n:Company) REQUIRE n.creditCode IS UNIQUE",
        ),
    ),
    Migration(
        version=3,
        description="范围索引：持股比例与事件日期",
        statements=(
            "CREATE INDEX hold_percent IF NOT EXISTS FOR (n)-[r:HOLD]->(m) ON (r.percent)",
            "CREATE INDEX event_occurred_date IF NOT EXISTS FOR (n:Event) ON (n.occurredDate)",
            "CREATE INDEX event_type IF NOT EXISTS FOR (n:Event) ON (n.eventType)",
            "CREATE INDEX guarantee_amount IF NOT EXISTS FOR (n)-[r:GUARANTEE]->(m) "
            "ON (r.guaranteed_amount)",
        ),
    ),
    Migration(
        version=4,
        description="文本索引与风险分索引",
        statements=(
            "CREATE INDEX company_name IF NOT EXISTS FOR (n:Company) ON (n.name)",
            "CREATE INDEX person_name IF NOT EXISTS FOR (n:Person) ON (n.name)",
            "CREATE INDEX company_risk_score IF NOT EXISTS FOR (n:Company) ON (n.riskScore)",
            "CREATE INDEX product_name IF NOT EXISTS FOR (n:Product) ON (n.name)",
            "CREATE INDEX company_industry IF NOT EXISTS FOR (n:Company) ON (n.industry)",
        ),
    ),
    Migration(
        version=5,
        description="关系类型索引与迁移记录",
        statements=(
            "CREATE INDEX relation_type IF NOT EXISTS FOR ()-[r]-() ON (r.relationType)",
            "CREATE INDEX entity_created_at IF NOT EXISTS FOR (n:Entity) ON (n.createdAt)",
        ),
    ),
)

LATEST_SCHEMA_VERSION = max(migration.version for migration in MIGRATIONS)


def constraint_statements() -> tuple[str, ...]:
    """Only the constraint statements — the minimum a write path needs."""
    return tuple(
        statement
        for migration in MIGRATIONS
        for statement in migration.statements
        if statement.startswith("CREATE CONSTRAINT")
    )


def index_statements() -> tuple[str, ...]:
    """Only the index statements."""
    return tuple(
        statement
        for migration in MIGRATIONS
        for statement in migration.statements
        if statement.startswith("CREATE INDEX")
    )


def describe_schema() -> str:
    """A human-readable schema summary used by the docs and the CLI."""
    lines: list[str] = ["Node labels:"]
    for label in ("Company", "Person", "Event", "Product"):
        lines.append(f"  :{label}")
    lines.append("")
    lines.append("Relation types:")
    for relation in RelationType:
        schema = RELATION_SCHEMA[relation]
        required = ", ".join(sorted(schema["required"])) or "-"
        lines.append(
            f"  {relation.value:<20} {schema['from']} -> {schema['to']} "
            f"| required: {required} | transmissibility: {schema['transmissibility']}"
        )
    lines.append("")
    lines.append("Risk indicators:")
    for spec in RISK_INDICATORS:
        lines.append(
            f"  {spec.id:<14} {spec.name:<18} weight={spec.weight:.2f} "
            f"v{spec.version} [{spec.category.value}]"
        )
    return "\n".join(lines)


__all__ = [
    "LATEST_SCHEMA_VERSION",
    "MIGRATIONS",
    "Migration",
    "constraint_statements",
    "describe_schema",
    "index_statements",
]

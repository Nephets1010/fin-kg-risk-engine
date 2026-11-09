"""Guards on the graph schema: the index catalogue and the migration files.

Two things are guarded here.

First, the schema that the platform applies is defined in
``src/fin_kg/graph/schema.py``; the ``migrations/`` directory holds a
human-readable copy of the same statements. Nothing in the code reads that
directory, so the two can drift apart — and did: ``company_industry`` was
present in ``V004__lookup_indexes.cypher`` but missing from the Python
definition, which meant the index the data model documents was never
created in production.

Second, the index catalogue documented in ``docs/data-model.md`` must match
what the schema actually creates.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fin_kg.graph.schema import MIGRATIONS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def _statement_key(statement: str) -> str:
    """The identifying part of a DDL statement, without the guards.

    The two sources spell the same statement differently: the Python
    definition names the endpoints (``FOR (n)-[r:HOLD]->(m)``) while the
    file leaves them anonymous (``FOR ()-[r:HOLD]->()``). Both mean the
    same thing to Cypher, so the endpoint names are normalised away.
    """
    normalised = " ".join(statement.split()).upper()
    normalised = normalised.replace(" IF NOT EXISTS", "")
    normalised = re.sub(r"FOR\s*\(\w*\)-", "FOR ()-", normalised)
    normalised = re.sub(r"->\(\w*\)", "->()", normalised)
    # A directed pattern (``-[r]->``) covers fewer relationships than an
    # undirected one (``-[r]-``). The schema uses the undirected form, so
    # the arrow is normalised to a plain dash before comparing.
    normalised = normalised.replace("->", "-")
    return normalised


def _schema_statements() -> dict[int, set[str]]:
    grouped: dict[int, set[str]] = {}
    for migration in MIGRATIONS:
        keys = set()
        for statement in migration.statements:
            upper = statement.upper()
            if upper.startswith("CREATE INDEX") or upper.startswith("CREATE CONSTRAINT"):
                keys.add(_statement_key(statement))
        grouped[migration.version] = keys
    return grouped


def _file_statements(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    # The files use a multi-line form; collapse it before comparing.
    collapsed = " ".join(text.split())
    keys: set[str] = set()
    for match in re.finditer(
        r"CREATE (INDEX|CONSTRAINT) (\w+) IF NOT EXISTS\s+(.*?)(?=;)",
        collapsed,
        re.IGNORECASE,
    ):
        kind, name, body = match.groups()
        keys.add(_statement_key(f"CREATE {kind} {name} {body}"))
    return keys


class TestMigrationFilesMatchTheSchema:
    """``migrations/`` is a readable copy; it must not drift from the code."""

    def test_every_schema_migration_has_a_file(self) -> None:
        files = sorted(MIGRATIONS_DIR.glob("V*.cypher"))
        versions = {int(re.match(r"V(\d+)", path.name).group(1)) for path in files}
        assert versions == {m.version for m in MIGRATIONS}, "迁移文件与 schema.py 的版本集合不一致"

    @pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda m: f"V{m.version}")
    def test_the_file_contains_every_schema_statement(self, migration) -> None:
        candidates = [
            path
            for path in MIGRATIONS_DIR.glob("V*.cypher")
            if re.match(rf"V0*{migration.version}__", path.name)
        ]
        assert candidates, f"V{migration.version} 没有对应的迁移文件"

        on_disk = _file_statements(candidates[0])
        in_code = _schema_statements()[migration.version]
        missing = in_code - on_disk
        assert not missing, (
            f"V{migration.version} 的 {sorted(missing)} 在 schema.py 里有，"
            f"但 {candidates[0].name} 里没有"
        )

    @pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda m: f"V{m.version}")
    def test_the_file_has_no_extra_statement(self, migration) -> None:
        candidates = [
            path
            for path in MIGRATIONS_DIR.glob("V*.cypher")
            if re.match(rf"V0*{migration.version}__", path.name)
        ]
        on_disk = _file_statements(candidates[0])
        in_code = _schema_statements()[migration.version]
        extra = on_disk - in_code
        assert not extra, (
            f"V{migration.version} 的 {sorted(extra)} 在 {candidates[0].name} 里有，"
            f"但 schema.py 里没有——生产不会创建它们"
        )


class TestDocumentedIndexesExist:
    """``docs/data-model.md`` §6 must not promise indexes that never exist."""

    @staticmethod
    def _documented() -> list[tuple[str, str]]:
        text = (PROJECT_ROOT / "docs" / "data-model.md").read_text(encoding="utf-8")
        section = text.split("## 6. 索引与约束")[1].split("## 7.")[0]
        documented: list[tuple[str, str]] = []
        for line in section.splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            kind = cells[0].upper()
            if kind in ("RANGE", "TEXT"):
                for label, prop in re.findall(r"`(\w+)\.(\w+)`", cells[1]):
                    documented.append((label.lower(), prop.lower()))
        return documented

    @staticmethod
    def _created() -> set[tuple[str, str]]:
        created: set[tuple[str, str]] = set()
        for migration in MIGRATIONS:
            for statement in migration.statements:
                match = re.search(
                    r"CREATE INDEX \w+ IF NOT EXISTS FOR \(n:(\w+)\) ON \(n\.(\w+)\)",
                    statement,
                    re.IGNORECASE,
                )
                if match:
                    created.add((match.group(1).lower(), match.group(2).lower()))
                    continue
                match = re.search(
                    r"CREATE INDEX \w+ IF NOT EXISTS FOR \(\w+\)-\[r:(\w+)\]->\(\w+\) "
                    r"ON \(r\.(\w+)\)",
                    statement,
                    re.IGNORECASE,
                )
                if match:
                    created.add((match.group(1).lower(), match.group(2).lower()))
        return created

    def test_every_documented_index_is_created(self) -> None:
        created = self._created()
        missing = [
            (label, prop) for label, prop in self._documented() if (label, prop) not in created
        ]
        assert not missing, (
            f"文档声明了 {missing} 索引，但 schema 不会创建它们——生产上会退化为全表扫描"
        )

    def test_the_company_industry_index_exists(self) -> None:
        """Regression: the documented industry filter had no index behind it."""
        assert ("company", "industry") in self._created()

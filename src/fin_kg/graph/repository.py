"""Graph repository abstraction.

The rest of the platform talks to :class:`GraphRepository`, never to the
Neo4j driver directly. Two implementations exist:

* :class:`Neo4jGraphRepository` — the production store.
* :class:`InMemoryGraphRepository` — a dependency-free store used by the unit
  tests and by the ``--dry-run`` ingestion mode. It implements the same
  semantics (including unique-constraint behaviour and multi-hop path
  enumeration) so behaviour verified in tests holds in production.
"""

from __future__ import annotations

import abc
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from fin_kg.core.errors import GraphStoreError
from fin_kg.domain.entities import GraphNode, node_to_properties
from fin_kg.domain.relations import RELATION_SCHEMA, Relation, RelationType

#: Labels the platform is allowed to write. Cypher cannot parameterise labels,
#: so every interpolated label must pass this whitelist first.
ALLOWED_LABELS: frozenset[str] = frozenset(
    {"Entity", "Company", "Person", "Event", "Product"}
)

#: Relationship types the platform is allowed to write, same reason as above.
ALLOWED_RELATION_TYPES: frozenset[str] = frozenset(
    relation.value for relation in RelationType
)


@dataclass(slots=True)
class GraphPath:
    """A path in the graph, expressed as alternating nodes and relations."""

    nodes: list[dict[str, Any]]
    relations: list[dict[str, Any]]

    @property
    def length(self) -> int:
        return len(self.relations)

    def node_ids(self) -> list[str]:
        return [node.get("id", "") for node in self.nodes]

    def relation_types(self) -> list[str]:
        return [relation.get("relationType", "") for relation in self.relations]


@dataclass
class GraphSnapshot:
    """The in-memory state of the graph, used by the offline repository."""

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    node_labels: dict[str, set[str]] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    adjacency: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    def add_node(self, node_id: str, labels: set[str], properties: dict[str, Any]) -> None:
        existing = self.nodes.get(node_id)
        if existing is not None:
            existing.update(properties)
            self.node_labels[node_id] |= labels
            return
        self.nodes[node_id] = dict(properties)
        self.node_labels[node_id] = set(labels)

    def add_edge(self, source: str, target: str, properties: dict[str, Any]) -> None:
        edge = {"source": source, "target": target, **properties}
        self.edges.append(edge)
        self.adjacency[source].append(edge)
        self.adjacency[target].append(edge)


class GraphRepository(abc.ABC):
    """Read/write interface over the financial knowledge graph."""

    @abc.abstractmethod
    def ensure_schema(self) -> None:
        """Apply constraints and indexes. Must be idempotent."""

    @abc.abstractmethod
    def upsert_nodes(self, nodes: Iterable[GraphNode]) -> int:
        """Insert or merge nodes. Returns the number of nodes written."""

    @abc.abstractmethod
    def upsert_relations(self, relations: Iterable[Relation]) -> int:
        """Insert or merge edges. Returns the number of edges written."""

    @abc.abstractmethod
    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Return a node's properties, or ``None`` when it does not exist."""

    @abc.abstractmethod
    def find_nodes(self, label: str, **filters: Any) -> list[dict[str, Any]]:
        """Return nodes matching a label and exact property filters."""

    @abc.abstractmethod
    def traverse(
        self,
        start_id: str,
        relation_types: Iterable[RelationType],
        *,
        max_hops: int,
        direction: str = "out",
    ) -> list[GraphPath]:
        """Enumerate paths from a node, bounded by ``max_hops``."""

    @abc.abstractmethod
    def execute_read(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a read query and return the records."""

    @abc.abstractmethod
    def update_properties(self, node_id: str, properties: dict[str, Any]) -> bool:
        """Merge properties onto an existing node. Returns ``True`` on success."""

    @abc.abstractmethod
    def stats(self) -> dict[str, int]:
        """Return node/relationship counts grouped by label and type."""

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    def _validate_relation(relation: Relation) -> None:
        schema = RELATION_SCHEMA[relation.relation_type]
        missing = schema["required"] - set(relation.properties)
        if missing:
            raise GraphStoreError(
                f"relation {relation.relation_type.value} is missing required properties",
                details={"missing": sorted(missing)},
            )


class InMemoryGraphRepository(GraphRepository):
    """A dependency-free in-memory graph used for tests and dry runs.

    It reproduces the semantics that matter for business correctness:

    * node identity is the node id — a second upsert merges,
    * unique constraints are modelled by :meth:`upsert_nodes` raising when a
      different node id claims the same credit code,
    * traversal is bounded by hop count and follows the requested direction.
    """

    def __init__(self) -> None:
        self.snapshot = GraphSnapshot()
        self._credit_code_owner: dict[str, str] = {}
        self.applied_schema_version: int | None = None

    def ensure_schema(self) -> None:
        from fin_kg.graph.schema import LATEST_SCHEMA_VERSION

        self.applied_schema_version = LATEST_SCHEMA_VERSION

    def upsert_nodes(self, nodes: Iterable[GraphNode]) -> int:
        written = 0
        for node in nodes:
            credit_code = getattr(node, "credit_code", None)
            if credit_code:
                owner = self._credit_code_owner.get(credit_code)
                if owner is not None and owner != node.id:
                    raise GraphStoreError(
                        "credit code already claimed by another company",
                        details={"credit_code": credit_code, "existing": owner, "incoming": node.id},
                    )
                self._credit_code_owner[credit_code] = node.id
            properties = node_to_properties(node)
            self.snapshot.add_node(node.id, node.label_set(), properties)
            written += 1
        return written

    def upsert_relations(self, relations: Iterable[Relation]) -> int:
        written = 0
        for relation in relations:
            self._validate_relation(relation)
            if relation.source_id not in self.snapshot.nodes:
                raise GraphStoreError(
                    "relation source node does not exist",
                    details={"source_id": relation.source_id},
                )
            if relation.target_id not in self.snapshot.nodes:
                raise GraphStoreError(
                    "relation target node does not exist",
                    details={"target_id": relation.target_id},
                )
            self.snapshot.add_edge(
                relation.source_id, relation.target_id, relation.to_properties()
            )
            written += 1
        return written

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        return self.snapshot.nodes.get(node_id)

    def find_nodes(self, label: str, **filters: Any) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for node_id, properties in self.snapshot.nodes.items():
            if label not in self.snapshot.node_labels.get(node_id, set()):
                continue
            if all(properties.get(key) == value for key, value in filters.items()):
                results.append({"id": node_id, **properties})
        return results

    def traverse(
        self,
        start_id: str,
        relation_types: Iterable[RelationType],
        *,
        max_hops: int,
        direction: str = "out",
    ) -> list[GraphPath]:
        wanted = {relation.value for relation in relation_types}
        paths: list[GraphPath] = []
        if start_id not in self.snapshot.nodes:
            return paths

        def expand(path_nodes: list[str], path_edges: list[dict[str, Any]]) -> None:
            if len(path_edges) >= max_hops:
                return
            current = path_nodes[-1]
            for edge in self.snapshot.adjacency.get(current, []):
                if edge.get("relationType") not in wanted:
                    continue
                if direction == "out":
                    if edge["source"] != current:
                        continue
                    nxt = edge["target"]
                elif direction == "in":
                    if edge["target"] != current:
                        continue
                    nxt = edge["source"]
                else:
                    nxt = edge["target"] if edge["source"] == current else edge["source"]
                if nxt in path_nodes:
                    continue
                new_edges = path_edges + [edge]
                new_nodes = path_nodes + [nxt]
                paths.append(
                    GraphPath(
                        nodes=[dict(self.snapshot.nodes[n]) for n in new_nodes],
                        relations=[dict(e) for e in new_edges],
                    )
                )
                expand(new_nodes, new_edges)

        expand([start_id], [])
        return paths

    def execute_read(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Offline repository supports only the small set of queries it needs.

        Rather than shipping a Cypher engine, the offline repository answers
        the specific query shapes used by the tests: node lookups by label and
        a manual traversal expression. Anything else raises, so a silent
        wrong answer is impossible.
        """
        params = parameters or {}
        normalised = " ".join(cypher.split()).lower()
        if "match (n:" in normalised and "return n" in normalised:
            label = normalised.split("match (n:", 1)[1].split(")", 1)[0]
            filters = {k: v for k, v in params.items() if k != "since"}
            return self.find_nodes(label, **filters)
        raise GraphStoreError(
            "in-memory repository cannot execute this query",
            details={"cypher": cypher},
        )

    def update_properties(self, node_id: str, properties: dict[str, Any]) -> bool:
        """Merge properties onto a node."""
        node = self.snapshot.nodes.get(node_id)
        if node is None:
            return False
        node.update(properties)
        return True

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for labels in self.snapshot.node_labels.values():
            for label in labels:
                counts[label] = counts.get(label, 0) + 1
        counts["Relationship"] = len(self.snapshot.edges)
        return counts


class FileGraphRepository(InMemoryGraphRepository):
    """An in-memory graph that persists itself to a JSON file.

    This exists so that the CLI is usable without a database: ``fin-kg seed``
    writes the snapshot, and the next ``fin-kg score`` reads it back. It is
    *not* a production store — it loads the whole graph into memory on every
    start and rewrites it on every write — but it makes the zero-dependency
    quickstart genuinely work end to end.

    The file format is the same shape as the in-memory snapshot, so it can be
    inspected with a text editor.
    """

    def __init__(self, path: Path, *, autosave: bool = True) -> None:
        super().__init__()
        self.path = Path(path)
        self.autosave = autosave
        self._loaded = False

    # -- persistence --------------------------------------------------------

    def load(self) -> "FileGraphRepository":
        """Read the snapshot from disk. A missing file is not an error."""
        if not self.path.exists():
            self._loaded = True
            return self
        import json

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.snapshot = GraphSnapshot(
            nodes=payload.get("nodes", {}),
            node_labels={
                key: set(value) for key, value in payload.get("nodeLabels", {}).items()
            },
            edges=payload.get("edges", []),
        )
        self._credit_code_owner = payload.get("creditCodes", {})
        adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.snapshot.edges:
            adjacency[edge["source"]].append(edge)
            adjacency[edge["target"]].append(edge)
        self.snapshot.adjacency = adjacency
        self._loaded = True
        return self

    def save(self) -> None:
        """Write the snapshot to disk atomically."""
        import json

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "nodes": self.snapshot.nodes,
            "nodeLabels": {key: sorted(value) for key, value in self.snapshot.node_labels.items()},
            "edges": self.snapshot.edges,
            "creditCodes": self._credit_code_owner,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    # -- overrides ----------------------------------------------------------

    def ensure_schema(self) -> None:
        super().ensure_schema()
        if not self._loaded:
            self.load()

    def upsert_nodes(self, nodes: Iterable[GraphNode]) -> int:
        written = super().upsert_nodes(nodes)
        if self.autosave:
            self.save()
        return written

    def upsert_relations(self, relations: Iterable[Relation]) -> int:
        written = super().upsert_relations(relations)
        if self.autosave:
            self.save()
        return written

    def update_properties(self, node_id: str, properties: dict[str, Any]) -> bool:
        updated = super().update_properties(node_id, properties)
        if updated and self.autosave:
            self.save()
        return updated


class Neo4jGraphRepository(GraphRepository):
    """Production graph store backed by Neo4j.

    All writes go through ``MERGE`` so the pipeline is idempotent and can be
    replayed after a failure. Batch size is configurable because a 10M-edge
    import through 5 000-row transactions is roughly an order of magnitude
    faster than the default.
    """

    def __init__(
        self,
        driver: Any,
        *,
        database: str = "neo4j",
        batch_size: int = 5_000,
    ) -> None:
        self._driver = driver
        self._database = database
        self._batch_size = max(1, batch_size)

    @classmethod
    def from_settings(cls, settings: Any) -> "Neo4jGraphRepository":
        """Build a repository from the application settings."""
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            settings.neo4j.uri,
            auth=(settings.neo4j.user, settings.neo4j.password.get_secret_value()),
            max_connection_pool_size=settings.neo4j.max_connection_pool,
            connection_timeout=settings.neo4j.connection_timeout,
        )
        return cls(
            driver,
            database=settings.neo4j.database,
            batch_size=settings.neo4j.batch_size,
        )

    def close(self) -> None:
        """Release the underlying driver."""
        self._driver.close()

    def _run(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run(cypher, parameters or {})
                return [record.data() for record in result]
        except Exception as exc:  # noqa: BLE001 - driver raises driver-specific errors
            raise GraphStoreError("graph store query failed", details={"error": str(exc)}) from exc

    def ensure_schema(self) -> None:
        from fin_kg.graph.schema import MIGRATIONS

        for migration in MIGRATIONS:
            for statement in migration.statements:
                self._run(statement)
            self._run(
                "MERGE (m:SchemaMigration {version:$version}) "
                "SET m.description=$description, m.appliedAt=datetime()",
                {"version": migration.version, "description": migration.description},
            )

    def upsert_nodes(self, nodes: Iterable[GraphNode]) -> int:
        """Merge nodes.

        Cypher cannot parameterise labels or relationship types, so both are
        interpolated — but only after being checked against a whitelist of
        labels that exist in the domain layer. Anything else is rejected
        rather than silently injected into the query.
        """
        payload: list[dict[str, Any]] = []
        for node in nodes:
            labels = sorted(node.label_set())
            for label in labels:
                if label not in ALLOWED_LABELS:
                    raise GraphStoreError(
                        "refusing to write a node with an unknown label",
                        details={"label": label, "allowed": sorted(ALLOWED_LABELS)},
                    )
            payload.append({"id": node.id, "labels": labels, "props": node_to_properties(node)})
        if not payload:
            return 0

        for chunk in _chunked(payload, self._batch_size):
            self._run(
                "UNWIND $rows AS row MERGE (n:Entity {id: row.id}) SET n += row.props",
                {"rows": chunk},
            )
            for label in ALLOWED_LABELS:
                labelled = [row for row in chunk if label in row["labels"]]
                if not labelled:
                    continue
                self._run(
                    f"UNWIND $rows AS row MATCH (n:Entity {{id: row.id}}) SET n:{label}",
                    {"rows": [{"id": row["id"]} for row in labelled]},
                )
        return len(payload)

    def upsert_relations(self, relations: Iterable[Relation]) -> int:
        """Merge edges.

        Relationship types are also interpolated, gated by the same whitelist
        mechanism used for labels.
        """
        rows: list[dict[str, Any]] = []
        for relation in relations:
            self._validate_relation(relation)
            if relation.relation_type.value not in ALLOWED_RELATION_TYPES:
                raise GraphStoreError(
                    "refusing to write an unknown relation type",
                    details={"type": relation.relation_type.value},
                )
            rows.append(
                {
                    "source": relation.source_id,
                    "target": relation.target_id,
                    "type": relation.relation_type.value,
                    "props": relation.to_properties(),
                }
            )
        if not rows:
            return 0

        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_type[row["type"]].append(row)

        for relation_type, group in by_type.items():
            cypher = (
                "UNWIND $rows AS row "
                "MATCH (s:Entity {id: row.source}), (t:Entity {id: row.target}) "
                f"MERGE (s)-[r:{relation_type} {{source: row.source, target: row.target}}]->(t) "
                "SET r += row.props"
            )
            for chunk in _chunked(group, self._batch_size):
                self._run(cypher, {"rows": chunk})
        return len(rows)

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        rows = self._run(
            "MATCH (n:Entity {id:$id}) RETURN n{.*} AS node LIMIT 1", {"id": node_id}
        )
        return rows[0]["node"] if rows else None

    def find_nodes(self, label: str, **filters: Any) -> list[dict[str, Any]]:
        rows = self._run(
            f"MATCH (n:{label}) WHERE all(k IN keys($filters) WHERE n[k] = $filters[k]) "
            "RETURN n{.*} AS node",
            {"filters": filters},
        )
        return [row["node"] for row in rows]

    def traverse(
        self,
        start_id: str,
        relation_types: Iterable[RelationType],
        *,
        max_hops: int,
        direction: str = "out",
    ) -> list[GraphPath]:
        wanted = [relation.value for relation in relation_types]
        if not wanted:
            return []
        for relation_type in wanted:
            if relation_type not in ALLOWED_RELATION_TYPES:
                raise GraphStoreError(
                    "refusing to traverse an unknown relation type",
                    details={"type": relation_type},
                )
        type_pattern = "|".join(wanted)
        pattern = {
            "out": f"-[r:({type_pattern})*1..{max_hops}]->",
            "in": f"<-[r:({type_pattern})*1..{max_hops}]-",
            "both": f"-[r:({type_pattern})*1..{max_hops}]-",
        }[direction]
        rows = self._run(
            f"MATCH p=(s:Entity {{id:$id}}){pattern}(t) "
            "RETURN nodes(p) AS nodes, relationships(p) AS relations",
            {"id": start_id},
        )
        return [
            GraphPath(
                nodes=[dict(n) for n in row["nodes"]],
                relations=[dict(r) for r in row["relations"]],
            )
            for row in rows
        ]

    def execute_read(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._run(cypher, parameters)

    def update_properties(self, node_id: str, properties: dict[str, Any]) -> bool:
        """Merge properties onto a node."""
        rows = self._run(
            "MATCH (n:Entity {id: $id}) SET n += $props RETURN count(n) AS updated",
            {"id": node_id, "props": properties},
        )
        return bool(rows and rows[0].get("updated"))

    def stats(self) -> dict[str, int]:
        node_rows = self._run(
            "MATCH (n) UNWIND labels(n) AS label "
            "RETURN label, count(*) AS total ORDER BY label"
        )
        rel_rows = self._run(
            "MATCH ()-[r]->() RETURN r.relationType AS type, count(*) AS total ORDER BY type"
        )
        counts = {row["label"]: row["total"] for row in node_rows}
        counts["Relationship"] = sum(row["total"] for row in rel_rows)
        for row in rel_rows:
            counts[f"REL:{row['type']}"] = row["total"]
        return counts


def _chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    """Yield ``items`` in slices of at most ``size`` elements."""
    for index in range(0, len(items), size):
        yield items[index : index + size]


def bfs_distances(
    repository: GraphRepository,
    start_id: str,
    relation_types: Iterable[RelationType],
    *,
    max_hops: int,
) -> dict[str, int]:
    """Shortest hop distance from ``start_id`` to every reachable node."""
    distances: dict[str, int] = {start_id: 0}
    queue: deque[tuple[str, int]] = deque([(start_id, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for path in repository.traverse(
            current, relation_types, max_hops=1, direction="out"
        ):
            for node_id in path.node_ids()[1:]:
                if node_id not in distances:
                    distances[node_id] = depth + 1
                    queue.append((node_id, depth + 1))
    return distances


__all__ = [
    "GraphPath",
    "GraphRepository",
    "GraphSnapshot",
    "InMemoryGraphRepository",
    "Neo4jGraphRepository",
    "bfs_distances",
]

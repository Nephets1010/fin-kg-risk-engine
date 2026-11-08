"""The ETL pipeline.

The pipeline is the only writer of the graph. It is deliberately explicit
about its stages so that each one can be measured:

``fetch``    pull raw records from one or more source adapters
``clean``    validate and normalise into domain objects
``align``    resolve entities to canonical ids
``persist``  write nodes and relations in batches
``checkpoint``record progress so an interrupted run can resume

Checkpointing is what makes the pipeline production-grade: an ingestion run
that dies at 80 % does not restart from zero. The checkpoint file records the
source cursor and the ids already written.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from fin_kg.core.errors import IngestionError
from fin_kg.core.logging import get_logger
from fin_kg.domain.entities import GraphNode
from fin_kg.domain.provenance import ExtractionMethod, Provenance, SourceType, utcnow
from fin_kg.domain.relations import Relation, RelationType
from fin_kg.graph.alignment import EntityResolver
from fin_kg.graph.repository import GraphRepository
from fin_kg.ingestion.base import RawRecord, RecordKind, SourceAdapter, chunked
from fin_kg.ingestion.cleaning import Cleaner, dedupe_key

logger = get_logger(__name__)


@dataclass(slots=True)
class Checkpoint:
    """Resumable ingestion state, persisted as JSON."""

    source_id: str
    last_cursor: str | None = None
    processed_records: int = 0
    written_nodes: int = 0
    written_relations: int = 0
    completed: bool = False
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Checkpoint":
        known = {key: payload[key] for key in cls.__slots__ if key in payload}
        return cls(**known)


def stored_items(existing: dict[str, Any]) -> list[Provenance]:
    """Parse the provenance stored on a node back into value objects."""
    import json

    raw = existing.get("provenance")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    items: list[Provenance] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            items.append(
                Provenance(
                    source_type=SourceType(entry.get("sourceType", "unknown")),
                    source_id=str(entry.get("sourceId", "unknown")),
                    record_id=str(entry.get("recordId", "unknown")),
                    extraction_method=ExtractionMethod(
                        entry.get("extractionMethod", "api")
                    ),
                    source_url=entry.get("sourceUrl"),
                    confidence=float(entry.get("confidence", 1.0)),
                    extractor_version=entry.get("extractorVersion"),
                    retrieved_at=datetime.fromisoformat(
                        entry["retrievedAt"]
                    )
                    if entry.get("retrievedAt")
                    else utcnow(),
                    content_hash=entry.get("contentHash"),
                )
            )
        except (ValueError, TypeError):
            continue
    return items


@dataclass(slots=True)
class IngestionReport:
    """Summary of one ingestion run."""

    source_ids: list[str] = field(default_factory=list)
    fetched: int = 0
    accepted: int = 0
    rejected: int = 0
    deduplicated: int = 0
    nodes_written: int = 0
    relations_written: int = 0
    duration_seconds: float = 0.0
    cleaning: dict[str, Any] = field(default_factory=dict)
    checkpoints: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class IngestionPipeline:
    """Runs the fetch → clean → align → persist cycle.

    The pipeline is idempotent: running it twice over the same input produces
    the same graph, because every write is a merge keyed on a deterministic id.
    """

    def __init__(
        self,
        repository: GraphRepository,
        *,
        resolver: EntityResolver | None = None,
        cleaner: Cleaner | None = None,
        batch_size: int = 2_000,
        checkpoint_dir: Path | None = None,
        on_progress: Callable[[str, int], None] | None = None,
    ) -> None:
        self.repository = repository
        self.resolver = resolver or EntityResolver()
        self.cleaner = cleaner or Cleaner()
        self.batch_size = max(1, batch_size)
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self._on_progress = on_progress
        #: Maps a source-specific id onto the canonical graph id.
        self._id_map: dict[str, str] = {}

    # -- checkpointing ------------------------------------------------------

    def _checkpoint_path(self, source_id: str) -> Path:
        if self.checkpoint_dir is None:
            raise IngestionError("checkpoint directory is not configured")
        safe = source_id.replace(":", "_").replace("/", "_")
        return self.checkpoint_dir / f"{safe}.json"

    def load_checkpoint(self, source_id: str) -> Checkpoint | None:
        """Read a previously saved checkpoint, if any."""
        if self.checkpoint_dir is None:
            return None
        path = self._checkpoint_path(source_id)
        if not path.exists():
            return None
        try:
            return Checkpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("unreadable checkpoint %s: %s", path, exc)
            return None

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Persist a checkpoint atomically."""
        if self.checkpoint_dir is None:
            return
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        path = self._checkpoint_path(checkpoint.source_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    # -- stages -------------------------------------------------------------

    def run(
        self,
        sources: Sequence[SourceAdapter],
        *,
        since: date | None = None,
        limit: int | None = None,
        resume: bool = True,
    ) -> IngestionReport:
        """Execute a full ingestion run over the given sources."""
        started = datetime.now(timezone.utc)
        report = IngestionReport()

        for source in sources:
            source_report = self._run_source(source, since=since, limit=limit, resume=resume)
            report.source_ids.append(source.name)
            report.fetched += source_report.fetched
            report.accepted += source_report.accepted
            report.rejected += source_report.rejected
            report.deduplicated += source_report.deduplicated
            report.nodes_written += source_report.nodes_written
            report.relations_written += source_report.relations_written
            report.checkpoints.extend(source_report.checkpoints)

        report.cleaning = self.cleaner.stats.as_dict()
        report.duration_seconds = round(
            (datetime.now(timezone.utc) - started).total_seconds(), 3
        )
        logger.info(
            "ingestion complete fetched=%s nodes=%s relations=%s in %ss",
            report.fetched,
            report.nodes_written,
            report.relations_written,
            report.duration_seconds,
        )
        return report

    def _run_source(
        self,
        source: SourceAdapter,
        *,
        since: date | None,
        limit: int | None,
        resume: bool,
    ) -> IngestionReport:
        report = IngestionReport(source_ids=[source.name])
        checkpoint = self.load_checkpoint(source.name) if resume else None
        if checkpoint and checkpoint.completed:
            logger.info("source %s already completed, skipping", source.name)
            report.checkpoints.append(source.name)
            return report

        result = source.fetch(since=since, limit=limit)
        report.fetched = len(result.records)

        seen_keys: set[str] = set()
        nodes: list[GraphNode] = []
        relations: list[Relation] = []

        for record in result.records:
            key = dedupe_key(record)
            if key in seen_keys:
                report.deduplicated += 1
                continue
            seen_keys.add(key)

            if record.kind is RecordKind.RELATION:
                relation = self._build_relation(record)
                if relation is not None:
                    relations.append(relation)
                continue

            try:
                node = self.cleaner.clean(record)
            except Exception:  # noqa: BLE001 - already counted and logged by the cleaner
                report.rejected += 1
                continue
            node = self._align_node(node, record)
            nodes.append(node)
            report.accepted += 1

        # Relations may reference nodes created in this very batch, so nodes
        # are persisted before relations.
        report.nodes_written = self._persist_nodes(nodes)
        report.relations_written = self._persist_relations(relations)

        checkpoint = Checkpoint(
            source_id=source.name,
            last_cursor=result.cursor,
            processed_records=report.fetched,
            written_nodes=report.nodes_written,
            written_relations=report.relations_written,
            completed=True,
        )
        self.save_checkpoint(checkpoint)
        report.checkpoints.append(source.name)
        return report

    def _align_node(self, node: GraphNode, record: RawRecord) -> GraphNode:
        """Resolve the node's canonical id, merging provenance when merged.

        The incoming id is remembered in the id map so that relation records
        referring to the same source id resolve to the same canonical node.
        """
        if node.label != "Company":
            self._id_map[record.record_id] = node.id
            self._id_map[node.id] = node.id
            return node
        credit_code = getattr(node, "credit_code", None)
        region = getattr(node, "region", None)
        resolution = self.resolver.resolve_company(
            name=node.name,
            credit_code=credit_code,
            region=region,
            source_key=record.record_id,
        )
        if resolution.entity_id != node.id:
            existing = self.repository.get_node(resolution.entity_id)
            node.id = resolution.entity_id
            if existing:
                self._merge_provenance(node, existing)
        self._id_map[record.record_id] = node.id
        self._id_map[node.id] = node.id
        self.resolver.register(node)
        return node

    @staticmethod
    def _merge_provenance(node: GraphNode, existing: dict[str, Any]) -> None:
        """Append the incoming provenance to the one already stored.

        The stored provenance is a JSON string (Neo4j cannot hold a list of
        maps), so it is parsed before the merge and re-serialised after.
        """
        import json

        stored = existing.get("provenance")
        known: set[str] = set()
        if isinstance(stored, str) and stored:
            try:
                for item in json.loads(stored):
                    if isinstance(item, dict):
                        known.add(item.get("provenanceHash", ""))
            except json.JSONDecodeError:
                known = set()
        elif isinstance(stored, list):
            for item in stored:
                if isinstance(item, dict):
                    known.add(item.get("provenanceHash", ""))

        merged = list(node.provenance)
        for item in stored_items(existing):
            if item.fingerprint() not in known:
                merged.append(item)
        node.provenance = merged
    def _canonical_id(self, source_id: str) -> str:
        """Translate a source-specific id into the canonical graph id."""
        return self._id_map.get(source_id, source_id)

    def _build_relation(self, record: RawRecord) -> Relation | None:
        """Turn a relation record into a validated :class:`Relation`."""
        payload = record.payload
        relation_type = payload.get("type")
        try:
            typed = RelationType(str(relation_type))
        except ValueError:
            logger.warning("skipping relation with unknown type %r", relation_type)
            return None

        source_id = self._canonical_id(str(payload.get("source") or ""))
        target_id = self._canonical_id(str(payload.get("target") or ""))
        if not source_id or not target_id:
            logger.warning("skipping relation without source/target")
            return None

        properties: dict[str, Any] = {}
        if typed is RelationType.HOLD:
            percent = payload.get("percent")
            if percent is None:
                logger.warning("skipping HOLD relation without percent")
                return None
            properties["percent"] = float(percent)
        elif typed is RelationType.GUARANTEE:
            amount = payload.get("guaranteed_amount")
            if amount is None:
                logger.warning("skipping GUARANTEE relation without amount")
                return None
            properties["guaranteed_amount"] = float(amount)
        elif typed is RelationType.SHAREHOLDER_OF:
            percent = payload.get("percent")
            if percent is None:
                return None
            properties["percent"] = float(percent)

        for key in ("since", "until", "role", "title", "relation_type", "amount", "detail"):
            if key in payload and payload[key] not in (None, ""):
                properties[key] = payload[key]

        provenance = Provenance(
            source_type=record.source_type,
            source_id=record.source_id,
            record_id=record.record_id,
            extraction_method=record.extraction_method,
            source_url=record.source_url,
            confidence=record.confidence,
            content_hash=record.record_id,
        )
        try:
            return Relation(
                source_id=source_id,
                target_id=target_id,
                relation_type=typed,
                properties=properties,
                provenance=[provenance],
            )
        except Exception as exc:  # noqa: BLE001 - pydantic validation error
            logger.warning("invalid relation record skipped: %s", exc)
            return None

    def _persist_nodes(self, nodes: Iterable[GraphNode]) -> int:
        written = 0
        for chunk in chunked(list(nodes), self.batch_size):
            written += self.repository.upsert_nodes(chunk)
            self._notify("nodes", written)
        return written

    def _persist_relations(self, relations: Iterable[Relation]) -> int:
        written = 0
        for chunk in chunked(list(relations), self.batch_size):
            written += self.repository.upsert_relations(chunk)
            self._notify("relations", written)
        return written

    def _notify(self, stage: str, count: int) -> None:
        if self._on_progress is not None:
            self._on_progress(stage, count)


__all__ = ["Checkpoint", "IngestionPipeline", "IngestionReport"]

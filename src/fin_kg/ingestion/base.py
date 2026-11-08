"""Source adapter contract.

A *source adapter* turns an external data feed into :class:`RawRecord`
objects. Adapters never write to the graph and never see the risk engine;
they only produce normalised, provenance-stamped records.

The adapter contract is intentionally small so that adding a new feed is a
matter of implementing :meth:`SourceAdapter.fetch` and registering it in
:data:`SOURCE_REGISTRY`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from fin_kg.domain.provenance import ExtractionMethod, SourceType


class RecordKind(str, Enum):
    """The kind of record a source emits."""

    COMPANY = "company"
    PERSON = "person"
    EVENT = "event"
    PRODUCT = "product"
    RELATION = "relation"


@dataclass(frozen=True, slots=True)
class RawRecord:
    """One record emitted by a source adapter, before cleaning.

    ``payload`` holds the source-specific fields; the cleaning layer maps them
    onto the domain models.
    """

    kind: RecordKind
    source_id: str
    record_id: str
    payload: dict[str, Any]
    source_type: SourceType = SourceType.UNKNOWN
    extraction_method: ExtractionMethod = ExtractionMethod.API
    source_url: str | None = None
    confidence: float = 1.0
    observed_at: datetime | None = None
    valid_time: date | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FetchResult:
    """The outcome of one fetch cycle."""

    records: list[RawRecord]
    has_more: bool = False
    cursor: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    stats: dict[str, Any] = field(default_factory=dict)


class SourceAdapter(abc.ABC):
    """Base class of every data source."""

    #: Stable identifier of the source, used in provenance records.
    name: str = "base"

    #: The kind of data the source provides.
    source_type: SourceType = SourceType.UNKNOWN

    @abc.abstractmethod
    def fetch(self, *, since: date | None = None, limit: int | None = None) -> FetchResult:
        """Return the records available since ``since``.

        Implementations must be side-effect free and safe to call repeatedly.
        """

    def healthcheck(self) -> bool:
        """Return ``True`` when the source is reachable."""
        return True

    def describe(self) -> dict[str, Any]:
        """Metadata about the source, surfaced by the API and the CLI."""
        return {"name": self.name, "sourceType": self.source_type.value}


class RecordBatch(BaseModel):
    """A validated batch of records ready for the graph writer."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    rows: list[dict[str, Any]] = Field(default_factory=list)


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """Split an iterable into chunks of at most ``size`` items."""
    chunk: list[Any] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


__all__ = [
    "FetchResult",
    "RawRecord",
    "RecordBatch",
    "RecordKind",
    "SourceAdapter",
    "chunked",
]

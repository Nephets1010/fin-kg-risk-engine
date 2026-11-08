"""Source provenance.

Every fact that enters the graph carries the provenance of the evidence that
produced it. Provenance is modelled as a first-class value object rather than a
free-text field so that it can be indexed, filtered and displayed.

Design notes
------------
* ``source_id`` is a stable logical identifier of the *source* (e.g.
  ``"tushare:basic"``), not of the record. The record-level identity lives in
  ``record_id``.
* ``confidence`` is the confidence of the *extraction*, not of the source. A
  hand-curated registry record and an LLM-extracted news snippet share the same
  source but very different confidence.
* ``extracted_at`` and ``valid_time`` differ: the former is when the platform
  learned the fact, the latter is when the fact became true in the world
  (bitemporal modelling).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SourceType(str, Enum):
    """The kind of system a fact originated from."""

    REGISTRY = "registry"          # 工商登记
    FILING = "filing"              # 公告 / 定期报告
    COURT = "court"                # 司法文书
    NEWS = "news"                  # 新闻舆情
    MARKET = "market"              # 行情与交易数据
    MANUAL = "manual"              # 人工录入
    SYNTHETIC = "synthetic"        # 合成/扩样数据
    DERIVED = "derived"            # 由平台计算派生
    UNKNOWN = "unknown"


class ExtractionMethod(str, Enum):
    """How the fact was obtained from the source."""

    API = "api"
    CRAWL = "crawl"
    CSV_IMPORT = "csv_import"
    LLM_EXTRACTION = "llm_extraction"
    RULE_MATCH = "rule_match"
    MANUAL_ENTRY = "manual_entry"
    SYNTHETIC = "synthetic"
    INHERITED = "inherited"


def utcnow() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


class Provenance(BaseModel):
    """Immutable provenance record attached to every node and edge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: SourceType = SourceType.UNKNOWN
    source_id: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=256)
    extraction_method: ExtractionMethod = ExtractionMethod.API
    source_url: str | None = Field(default=None, max_length=2048)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    extractor_version: str | None = Field(default=None, max_length=64)
    retrieved_at: datetime = Field(default_factory=utcnow)
    valid_time: date | None = None
    content_hash: str | None = Field(default=None, max_length=64)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("source_url must be an http(s) URL")
        return value

    @classmethod
    def synthetic(cls, *, source_id: str = "synthetic:generator", **kwargs: Any) -> "Provenance":
        """Build a provenance record for generated sample data."""
        defaults: dict[str, Any] = {
            "source_type": SourceType.SYNTHETIC,
            "source_id": source_id,
            "record_id": source_id,
            "extraction_method": ExtractionMethod.SYNTHETIC,
            "confidence": 0.5,
            "extractor_version": "synthetic-1.0",
        }
        defaults.update(kwargs)
        return cls(**defaults)

    @classmethod
    def derived(cls, *, source_id: str, record_id: str, **kwargs: Any) -> "Provenance":
        """Build a provenance record for a computed fact."""
        defaults: dict[str, Any] = {
            "source_type": SourceType.DERIVED,
            "source_id": source_id,
            "record_id": record_id,
            "extraction_method": ExtractionMethod.RULE_MATCH,
            "confidence": 1.0,
        }
        defaults.update(kwargs)
        return cls(**defaults)

    def fingerprint(self) -> str:
        """Stable identity of the *evidence*, used for deduplication."""
        material = f"{self.source_id}|{self.record_id}|{self.source_url or ''}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def to_properties(self) -> dict[str, Any]:
        """Flatten into Neo4j-friendly property primitives."""
        return {
            "sourceType": self.source_type.value,
            "sourceId": self.source_id,
            "recordId": self.record_id,
            "extractionMethod": self.extraction_method.value,
            "sourceUrl": self.source_url,
            "confidence": self.confidence,
            "extractorVersion": self.extractor_version,
            "retrievedAt": self.retrieved_at.isoformat(),
            "validTime": self.valid_time.isoformat() if self.valid_time else None,
            "contentHash": self.content_hash,
            "provenanceHash": self.fingerprint(),
        }


__all__ = ["ExtractionMethod", "Provenance", "SourceType", "utcnow"]

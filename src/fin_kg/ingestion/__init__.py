"""Ingestion layer: source adapters, cleaning and the ETL pipeline."""

from fin_kg.ingestion.base import (
    FetchResult,
    RawRecord,
    RecordBatch,
    RecordKind,
    SourceAdapter,
    chunked,
)
from fin_kg.ingestion.cleaning import (
    Cleaner,
    CleaningStats,
    classify_event_type,
    classify_industry,
    classify_person_role,
    classify_product_type,
    classify_severity,
    classify_status,
    dedupe_key,
    parse_amount,
    parse_date,
    parse_percent,
)
from fin_kg.ingestion.pipeline import Checkpoint, IngestionPipeline, IngestionReport
from fin_kg.ingestion.sources import (
    CsvSourceAdapter,
    JsonLinesSourceAdapter,
    SyntheticSourceAdapter,
    TushareSourceAdapter,
)

__all__ = [
    "Checkpoint",
    "Cleaner",
    "CleaningStats",
    "CsvSourceAdapter",
    "FetchResult",
    "IngestionPipeline",
    "IngestionReport",
    "JsonLinesSourceAdapter",
    "RawRecord",
    "RecordBatch",
    "RecordKind",
    "SourceAdapter",
    "SyntheticSourceAdapter",
    "TushareSourceAdapter",
    "chunked",
    "classify_event_type",
    "classify_industry",
    "classify_person_role",
    "classify_product_type",
    "classify_severity",
    "classify_status",
    "dedupe_key",
    "parse_amount",
    "parse_date",
    "parse_percent",
]

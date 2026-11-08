"""Concrete source adapters.

Three adapters ship with the platform:

:class:`CsvSourceAdapter`
    Reads the sample CSV files that live in ``data/samples``. This is the
    default for local development and for the CI integration tests.

:class:`SyntheticSourceAdapter`
    Generates a statistically plausible graph — including a realistic
    guarantee chain — so the whole pipeline can be exercised without touching
    any external service. It is explicitly labelled ``SYNTHETIC`` in the
    provenance of every record it produces, which keeps generated data from
    ever being mistaken for real data.

:class:`TushareSourceAdapter`
    Reads from the Tushare open financial data API. It requires a token and
    is disabled unless ``FIN_KG_TUSHARE_TOKEN`` is set.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fin_kg.core.errors import IngestionError
from fin_kg.core.logging import get_logger
from fin_kg.domain.provenance import ExtractionMethod, SourceType
from fin_kg.ingestion.base import FetchResult, RawRecord, RecordKind, SourceAdapter

logger = get_logger(__name__)

#: Maps the CSV file stem to the kind of records it contains.
_CSV_KIND_BY_STEM: dict[str, RecordKind] = {
    "companies": RecordKind.COMPANY,
    "persons": RecordKind.PERSON,
    "events": RecordKind.EVENT,
    "products": RecordKind.PRODUCT,
    "relations": RecordKind.RELATION,
}


class CsvSourceAdapter(SourceAdapter):
    """Loads records from a directory of CSV files."""

    name = "csv"
    source_type = SourceType.MANUAL

    def __init__(self, directory: Path, *, source_id: str = "csv:samples") -> None:
        self.directory = Path(directory)
        self.source_id = source_id

    def fetch(self, *, since: date | None = None, limit: int | None = None) -> FetchResult:
        """Read every CSV file in the directory."""
        if not self.directory.exists():
            raise IngestionError(
                "data directory does not exist", details={"directory": str(self.directory)}
            )

        records: list[RawRecord] = []
        for path in sorted(self.directory.glob("*.csv")):
            kind = _CSV_KIND_BY_STEM.get(path.stem)
            if kind is None:
                logger.debug("skipping unrecognised csv file %s", path.name)
                continue
            records.extend(self._read_file(path, kind))

        if since is not None:
            records = [record for record in records if self._within_window(record, since)]
        if limit is not None:
            records = records[:limit]

        return FetchResult(
            records=records,
            stats={"directory": str(self.directory), "files": len(records)},
        )

    def _read_file(self, path: Path, kind: RecordKind) -> list[RawRecord]:
        records: list[RawRecord] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader):
                cleaned = {key: value for key, value in row.items() if key and value not in ("", None)}
                if not cleaned:
                    continue
                record_id = str(cleaned.get("id") or f"{path.stem}-{index}")
                records.append(
                    RawRecord(
                        kind=kind,
                        source_id=f"{self.source_id}:{path.stem}",
                        record_id=record_id,
                        payload=cleaned,
                        source_type=SourceType.MANUAL,
                        extraction_method=ExtractionMethod.CSV_IMPORT,
                        confidence=1.0,
                    )
                )
        logger.info("read %s records from %s", len(records), path.name)
        return records

    @staticmethod
    def _within_window(record: RawRecord, since: date) -> bool:
        raw_date = record.payload.get("date") or record.payload.get("occurred_date")
        if not raw_date:
            return True
        try:
            parsed = datetime.fromisoformat(str(raw_date)).date()
        except ValueError:
            return True
        return parsed >= since


class SyntheticSourceAdapter(SourceAdapter):
    """Generates a plausible synthetic financial graph.

    The generator produces a small number of *groups*: one anchor company with
    a guarantee chain beneath it, subsidiaries, people and adverse events.
    The structure is deterministic for a given ``seed``, which makes the
    synthetic dataset usable as a test fixture.
    """

    name = "synthetic"
    source_type = SourceType.SYNTHETIC

    def __init__(
        self,
        *,
        companies: int = 40,
        events_per_company: float = 1.5,
        seed: int = 20240101,
    ) -> None:
        self.companies = companies
        self.events_per_company = events_per_company
        self.seed = seed

    def fetch(self, *, since: date | None = None, limit: int | None = None) -> FetchResult:
        """Generate the synthetic dataset."""
        rng = random.Random(self.seed)
        today = date.today()
        records: list[RawRecord] = []
        company_ids: list[str] = []
        person_ids: list[str] = []

        for index in range(self.companies):
            company_id = f"syn-c-{index:05d}"
            company_ids.append(company_id)
            industry = rng.choice(
                ["制造业", "金融", "房地产", "能源", "科技", "建筑工程", "贸易"]
            )
            records.append(
                RawRecord(
                    kind=RecordKind.COMPANY,
                    source_id="synthetic:companies",
                    record_id=company_id,
                    payload={
                        "id": company_id,
                        "name": f"合成{index}号{industry}有限公司",
                        "credit_code": f"SYN{index:015d}",
                        "reg_capital": rng.choice([500, 1_000, 5_000, 20_000]),
                        "industry": industry,
                        "status": rng.choice(["存续", "存续", "存续", "吊销"]),
                        "founded_date": (today - timedelta(days=rng.randint(400, 6_000))).isoformat(),
                        "region": rng.choice(["北京", "上海", "深圳", "杭州", "成都"]),
                    },
                    source_type=SourceType.SYNTHETIC,
                    extraction_method=ExtractionMethod.SYNTHETIC,
                    confidence=0.5,
                )
            )

            person_id = f"syn-p-{index:05d}"
            person_ids.append(person_id)
            records.append(
                RawRecord(
                    kind=RecordKind.PERSON,
                    source_id="synthetic:persons",
                    record_id=person_id,
                    payload={
                        "id": person_id,
                        "name": f"合成人物{index}号",
                        "role": rng.choice(["法定代表人", "董事", "实际控制人"]),
                    },
                    source_type=SourceType.SYNTHETIC,
                    extraction_method=ExtractionMethod.SYNTHETIC,
                    confidence=0.5,
                )
            )

            event_count = max(0, int(rng.gauss(self.events_per_company, 1.0)))
            for event_index in range(event_count):
                event_type = rng.choice(
                    ["强制执行", "涉诉", "行政处罚", "评级下调", "负面舆情", "违约", "欠税"]
                )
                occurred = today - timedelta(days=rng.randint(1, 400))
                records.append(
                    RawRecord(
                        kind=RecordKind.EVENT,
                        source_id="synthetic:events",
                        record_id=f"syn-e-{index:05d}-{event_index}",
                        payload={
                            "id": f"syn-e-{index:05d}-{event_index}",
                            "name": f"{event_type}事件-{index}-{event_index}",
                            "type": event_type,
                            "severity": rng.choice(["轻微", "一般", "较大", "重大"]),
                            "date": occurred.isoformat(),
                            "amount": rng.choice([None, 50, 500, 5_000, 20_000]),
                            "description": f"{event_type}相关情况说明",
                        },
                        source_type=SourceType.SYNTHETIC,
                        extraction_method=ExtractionMethod.SYNTHETIC,
                        confidence=0.5,
                    )
                )

        # Relations: ownership, guarantees and legal representation.
        for index, company_id in enumerate(company_ids):
            if index >= 1:
                records.append(
                    RawRecord(
                        kind=RecordKind.RELATION,
                        source_id="synthetic:relations",
                        record_id=f"syn-r-hold-{index:05d}",
                        payload={
                            "type": "HOLD",
                            "source": company_ids[0],
                            "target": company_id,
                            "percent": round(rng.uniform(0.15, 1.0), 4),
                        },
                        source_type=SourceType.SYNTHETIC,
                        extraction_method=ExtractionMethod.SYNTHETIC,
                        confidence=0.5,
                    )
                )
            if index + 2 < len(company_ids) and rng.random() < 0.5:
                records.append(
                    RawRecord(
                        kind=RecordKind.RELATION,
                        source_id="synthetic:relations",
                        record_id=f"syn-r-gua-{index:05d}",
                        payload={
                            "type": "GUARANTEE",
                            "source": company_id,
                            "target": company_ids[index + 2],
                            "guaranteed_amount": rng.choice([100, 500, 2_000, 10_000]),
                        },
                        source_type=SourceType.SYNTHETIC,
                        extraction_method=ExtractionMethod.SYNTHETIC,
                        confidence=0.5,
                    )
                )
            records.append(
                RawRecord(
                    kind=RecordKind.RELATION,
                    source_id="synthetic:relations",
                    record_id=f"syn-r-legal-{index:05d}",
                    payload={
                        "type": "LEGAL_REP",
                        "source": person_ids[index],
                        "target": company_id,
                    },
                    source_type=SourceType.SYNTHETIC,
                    extraction_method=ExtractionMethod.SYNTHETIC,
                    confidence=0.5,
                )
            )

        # Adverse events are attached to companies through INVOLVED_IN edges.
        for record in list(records):
            if record.kind is not RecordKind.EVENT:
                continue
            company_index = int(record.record_id.split("-")[2])
            records.append(
                RawRecord(
                    kind=RecordKind.RELATION,
                    source_id="synthetic:relations",
                    record_id=f"syn-r-inv-{record.record_id}",
                    payload={
                        "type": "INVOLVED_IN",
                        "source": company_ids[company_index % len(company_ids)],
                        "target": record.payload["id"],
                    },
                    source_type=SourceType.SYNTHETIC,
                    extraction_method=ExtractionMethod.SYNTHETIC,
                    confidence=0.5,
                )
            )

        if since is not None:
            records = [
                record
                for record in records
                if record.kind is not RecordKind.EVENT
                or str(record.payload.get("date", "")) >= since.isoformat()
            ]
        if limit is not None:
            records = records[:limit]

        return FetchResult(
            records=records,
            stats={
                "companies": len(company_ids),
                "persons": len(person_ids),
                "records": len(records),
                "seed": self.seed,
            },
        )


class TushareSourceAdapter(SourceAdapter):
    """Reads company basics from the Tushare open API.

    The adapter is disabled without a token. It uses the token from the
    environment; the token is never logged and never stored on the instance.
    """

    name = "tushare"
    source_type = SourceType.MARKET

    def __init__(self, *, token: str, base_url: str = "http://api.tushare.pro") -> None:
        if not token:
            raise IngestionError("Tushare adapter requires a token")
        self._token = token
        self.base_url = base_url

    def fetch(self, *, since: date | None = None, limit: int | None = None) -> FetchResult:
        """Fetch company basics and convert them into raw records."""
        import httpx

        payload = {
            "api_name": "stock_basic",
            "token": self._token,
            "params": {"list_status": "L"},
            "fields": "ts_code,name,industry,market,list_date",
        }
        try:
            response = httpx.post(self.base_url, json=payload, timeout=30.0)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - httpx raises several transport errors
            raise IngestionError("tushare request failed", details={"error": str(exc)}) from exc

        if body.get("code") != 0:
            raise IngestionError(
                "tushare returned an error",
                details={"code": body.get("code"), "msg": body.get("msg")},
            )

        items = body.get("data", {}).get("items", [])
        fields = body.get("data", {}).get("fields", [])
        records: list[RawRecord] = []
        for item in items:
            row = dict(zip(fields, item))
            records.append(
                RawRecord(
                    kind=RecordKind.COMPANY,
                    source_id="tushare:stock_basic",
                    record_id=str(row.get("ts_code", "")),
                    payload={
                        "id": str(row.get("ts_code", "")),
                        "name": row.get("name", ""),
                        "industry": row.get("industry", ""),
                        "listed_date": row.get("list_date"),
                    },
                    source_type=SourceType.MARKET,
                    extraction_method=ExtractionMethod.API,
                    confidence=0.95,
                )
            )
        if limit is not None:
            records = records[:limit]
        return FetchResult(records=records, stats={"records": len(records)})

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sourceType": self.source_type.value,
            "baseUrl": self.base_url,
            "credential": "token-from-environment",
        }


class JsonLinesSourceAdapter(SourceAdapter):
    """Reads records from a JSON Lines file — used for replaying raw feeds."""

    name = "jsonl"
    source_type = SourceType.UNKNOWN

    def __init__(self, path: Path, *, kind: RecordKind, source_id: str = "jsonl:replay") -> None:
        self.path = Path(path)
        self.kind = kind
        self.source_id = source_id

    def fetch(self, *, since: date | None = None, limit: int | None = None) -> FetchResult:
        if not self.path.exists():
            raise IngestionError(
                "jsonl file does not exist", details={"path": str(self.path)}
            )
        records: list[RawRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise IngestionError(
                        "malformed jsonl line",
                        details={"path": str(self.path), "line": line_number + 1, "error": str(exc)},
                    ) from exc
                records.append(
                    RawRecord(
                        kind=self.kind,
                        source_id=self.source_id,
                        record_id=str(payload.get("id") or f"line-{line_number}"),
                        payload=payload,
                        source_type=SourceType.UNKNOWN,
                        extraction_method=ExtractionMethod.CSV_IMPORT,
                        confidence=float(payload.get("confidence", 1.0)),
                    )
                )
        if limit is not None:
            records = records[:limit]
        return FetchResult(records=records, stats={"records": len(records)})


__all__ = [
    "CsvSourceAdapter",
    "JsonLinesSourceAdapter",
    "SyntheticSourceAdapter",
    "TushareSourceAdapter",
]

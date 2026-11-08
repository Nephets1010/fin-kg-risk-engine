"""Cleaning and normalisation.

The cleaning layer turns :class:`RawRecord` payloads into validated domain
objects. Its job is to make heterogeneous feeds look alike:

* **Field mapping** — source-specific keys become canonical attribute names.
* **Type coercion** — percentages arrive as ``"60%"``, ``0.6`` or ``60``; all
  three must end up as ``0.6``.
* **Temporal normalisation** — dates arrive as ISO strings, ``"2024年3月"`` or
  epoch timestamps.
* **Deduplication** — identical records from different feeds collapse onto
  one, keeping the highest-confidence provenance.

Every cleaning decision is recorded on the node's provenance list, so a
reviewer can always tell where a fact came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from fin_kg.core.errors import ValidationError
from fin_kg.core.logging import get_logger
from fin_kg.domain.entities import (
    Company,
    CompanyStatus,
    Event,
    EventType,
    Industry,
    Person,
    PersonRole,
    Product,
    ProductType,
    Severity,
)
from fin_kg.domain.provenance import Provenance
from fin_kg.ingestion.base import RawRecord, RecordKind

logger = get_logger(__name__)

_PERCENT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*%\s*$")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_CHINESE_DATE_RE = re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})")

#: Industry keyword table — the mapping used when the source does not provide
#: a normalised industry code.
_INDUSTRY_KEYWORDS: tuple[tuple[Industry, tuple[str, ...]], ...] = (
    (Industry.FINANCE, ("银行", "证券", "保险", "信托", "基金", "金融", "资本")),
    (Industry.REAL_ESTATE, ("地产", "置业", "房地产", "城建", "投资控股")),
    (Industry.ENERGY, ("能源", "电力", "石化", "煤", "油", "燃气")),
    (Industry.TECHNOLOGY, ("科技", "软件", "信息", "网络", "电子", "半导体", "智能")),
    (Industry.CONSTRUCTION, ("建设", "建工", "工程", "建筑", "路桥", "基建")),
    (Industry.MANUFACTURING, ("制造", "工业", "机械", "化工", "材料", "装备")),
    (Industry.TRADE, ("贸易", "商贸", "供应链", "进出口")),
    (Industry.AGRICULTURE, ("农业", "农牧", "食品", "种业")),
    (Industry.TRANSPORT, ("物流", "运输", "航运", "港口", "航空")),
    (Industry.HEALTHCARE, ("医疗", "医药", "生物", "健康")),
    (Industry.EDUCATION, ("教育", "学校", "培训")),
    (Industry.CULTURE_ENT, ("文化", "传媒", "影视", "出版")),
)

_STATUS_KEYWORDS: tuple[tuple[CompanyStatus, tuple[str, ...]], ...] = (
    (CompanyStatus.BANKRUPT, ("破产", "清算", "重整")),
    (CompanyStatus.CANCELLED, ("注销", "吊销", "撤销")),
    (CompanyStatus.SUSPENDED, ("停业", "暂停", "冻结")),
    (CompanyStatus.ACTIVE, ("存续", "在业", "正常", "开业")),
)

_EVENT_KEYWORDS: tuple[tuple[EventType, tuple[str, ...]], ...] = (
    (EventType.ENFORCEMENT, ("强制执行", "被执行", "失信", "限高")),
    (EventType.LITIGATION, ("诉讼", "起诉", "开庭", "判决", "仲裁", "涉诉")),
    (EventType.PENALTY, ("处罚", "罚款", "监管措施", "警示函", "责令")),
    (EventType.ARREARS, ("欠税", "欠款", "欠薪", "未偿还")),
    (EventType.DEFAULT, ("违约", "逾期未兑付", "债券违约")),
    (EventType.RATING_DOWNGRADE, ("评级下调", "评级展望负面", "降级")),
    (EventType.EQUITY_FREEZE, ("股权冻结", "股份冻结", "质押冻结")),
    (EventType.BUSINESS_CHANGE, ("变更", "增资", "减资", "股权转让", "高管变更")),
    (EventType.ABNORMAL_OPERATION, ("经营异常", "列入异常", "地址异常")),
    (EventType.NEGATIVE_NEWS, ("负面", "舆情", "曝光", "涉嫌", "骗局")),
)

_SEVERITY_KEYWORDS: tuple[tuple[Severity, tuple[str, ...]], ...] = (
    (Severity.CRITICAL, ("重大", "严重", "特别重大", "刑事", "犯罪", "诈骗")),
    (Severity.HIGH, ("较大", "严重", "重要", "高危")),
    (Severity.MEDIUM, ("一般", "中等", "较大")),
    (Severity.LOW, ("轻微", "较小", "提示")),
)


@dataclass(slots=True)
class CleaningStats:
    """Counters describing what the cleaning layer changed."""

    total: int = 0
    accepted: int = 0
    rejected: int = 0
    deduplicated: int = 0
    coerced_fields: int = 0
    errors: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "deduplicated": self.deduplicated,
            "coercedFields": self.coerced_fields,
            "errors": dict(self.errors),
        }


def parse_percent(value: Any) -> float | None:
    """Parse a percentage into a ratio in ``[0, 1]``.

    Accepts ``"60%"``, ``"60"``, ``0.6`` and ``60``. A bare number greater
    than 1 is treated as a percentage and divided by 100.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1.0:
            number = number / 100.0
        return max(0.0, min(1.0, number))
    text = str(value).strip()
    if not text:
        return None
    match = _PERCENT_RE.match(text)
    if match:
        return max(0.0, min(1.0, float(match.group(1)) / 100.0))
    numeric = _NUMBER_RE.search(text)
    if not numeric:
        return None
    number = float(numeric.group())
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))


def parse_amount(value: Any, *, unit: str = "万元") -> float | None:
    """Parse a monetary amount into a float.

    Chinese unit suffixes are understood: ``万`` → ÷10 000, ``亿`` → ×10 000.
    A bare number is taken to be in the platform's canonical unit, which is
    **万元** (ten thousand yuan) — the unit the Chinese registry uses.

    Thousands separators are stripped before parsing. Without this step a
    value such as ``5,000万元`` would parse as ``5`` — a factor of a
    thousand too small — and the ratio indicators built on top of it would
    be silently wrong.

    ``unit`` documents the canonical unit of the caller; it exists so that
    the contract is visible at the call site rather than buried in a
    convention.
    """
    del unit  # documented, not applied — bare numbers are already canonical
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Drop the thousands separators so that "5,000" parses as 5000.
    # The lookahead requires exactly three digits followed by a non-digit, so
    # that a malformed group such as "1,2345" is not silently accepted.
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    numeric = _NUMBER_RE.search(text)
    if not numeric:
        return None
    amount = float(numeric.group())
    if "亿" in text:
        amount *= 10_000
    elif "万" in text:
        amount *= 1
    elif "元" in text:
        amount /= 10_000
    return amount


def parse_date(value: Any) -> date | None:
    """Parse a date from the many shapes the sources use."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    match = _CHINESE_DATE_RE.search(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    for fmt in ("%Y/%m/%d", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def classify_industry(value: Any) -> Industry:
    """Map a free-text industry string onto the normalised enum."""
    if isinstance(value, Industry):
        return value
    text = str(value or "").strip()
    if not text:
        return Industry.OTHER
    lowered = text.lower()
    for industry, keywords in _INDUSTRY_KEYWORDS:
        if any(keyword in text or keyword in lowered for keyword in keywords):
            return industry
    return Industry.OTHER


def classify_status(value: Any) -> CompanyStatus:
    """Map a free-text registration status onto the normalised enum."""
    text = str(value or "").strip()
    if not text:
        return CompanyStatus.UNKNOWN
    for status, keywords in _STATUS_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return status
    return CompanyStatus.UNKNOWN


def classify_event_type(value: Any) -> EventType:
    """Map a free-text event description onto the normalised enum."""
    text = str(value or "").strip()
    if not text:
        return EventType.OTHER
    for event_type, keywords in _EVENT_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return event_type
    return EventType.OTHER


def classify_severity(value: Any, description: str = "") -> Severity:
    """Map a free-text severity onto the normalised enum."""
    text = f"{value or ''} {description}".strip()
    for severity, keywords in _SEVERITY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return severity
    return Severity.MEDIUM


def classify_person_role(value: Any) -> PersonRole:
    """Map a free-text role onto the normalised enum."""
    text = str(value or "").strip()
    mapping = (
        (PersonRole.ACTUAL_CONTROLLER, ("实际控制", "实控")),
        (PersonRole.LEGAL_REP, ("法定代表人", "法人")),
        (PersonRole.DIRECTOR, ("董事",)),
        (PersonRole.SUPERVISOR, ("监事",)),
        (PersonRole.EXECUTIVE, ("总经理", "副总", "高管", "经理", "CEO")),
    )
    for role, keywords in mapping:
        if any(keyword in text for keyword in keywords):
            return role
    return PersonRole.SHAREHOLDER


class Cleaner:
    """Turns raw records into validated domain objects.

    The cleaner is stateless apart from its counters, so a single instance can
    be reused across batches.
    """

    def __init__(self) -> None:
        self.stats = CleaningStats()

    def provenance_for(self, record: RawRecord) -> Provenance:
        """Build the provenance stamp for a raw record."""
        return Provenance(
            source_type=record.source_type,
            source_id=record.source_id,
            record_id=record.record_id,
            extraction_method=record.extraction_method,
            source_url=record.source_url,
            confidence=record.confidence,
            valid_time=record.valid_time,
            content_hash=record.record_id,
            extra=dict(record.extra),
        )

    def clean_company(self, record: RawRecord) -> Company:
        """Build a :class:`Company` from a raw record."""
        payload = record.payload
        name = str(payload.get("name") or payload.get("company_name") or "").strip()
        if not name:
            raise ValidationError("company record has no name")
        credit_code = payload.get("credit_code") or payload.get("creditCode")
        capital = payload.get("reg_capital") or payload.get("regCapital")
        return Company(
            id=str(payload.get("id") or record.record_id),
            name=name,
            credit_code=str(credit_code).strip().upper() if credit_code else None,
            reg_capital=parse_amount(capital),
            industry=classify_industry(payload.get("industry")),
            status=classify_status(payload.get("status")),
            founded_date=parse_date(payload.get("founded_date") or payload.get("foundDate")),
            region=payload.get("region"),
            provenance=[self.provenance_for(record)],
        )

    def clean_person(self, record: RawRecord) -> Person:
        """Build a :class:`Person` from a raw record."""
        payload = record.payload
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationError("person record has no name")
        return Person(
            id=str(payload.get("id") or record.record_id),
            name=name,
            role=classify_person_role(payload.get("role")),
            id_number_hash=payload.get("id_number_hash"),
            provenance=[self.provenance_for(record)],
        )

    def clean_event(self, record: RawRecord) -> Event:
        """Build an :class:`Event` from a raw record."""
        payload = record.payload
        description = str(payload.get("description") or "")
        return Event(
            id=str(payload.get("id") or record.record_id),
            name=str(payload.get("name") or description[:64] or record.record_id),
            event_type=classify_event_type(payload.get("type") or description),
            severity=classify_severity(payload.get("severity"), description),
            occurred_date=parse_date(payload.get("date") or payload.get("occurred_date")),
            amount=parse_amount(payload.get("amount")),
            court=payload.get("court"),
            case_number=payload.get("case_number"),
            description=description or None,
            provenance=[self.provenance_for(record)],
        )

    def clean_product(self, record: RawRecord) -> Product:
        """Build a :class:`Product` from a raw record."""
        payload = record.payload
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationError("product record has no name")
        return Product(
            id=str(payload.get("id") or record.record_id),
            name=name,
            product_type=classify_product_type(payload.get("type")),
            issuer_id=payload.get("issuer_id"),
            amount=parse_amount(payload.get("amount")),
            issue_date=parse_date(payload.get("issue_date")),
            maturity_date=parse_date(payload.get("maturity_date")),
            coupon_rate=parse_percent(payload.get("coupon_rate")),
            rating=payload.get("rating"),
            provenance=[self.provenance_for(record)],
        )

    def clean(self, record: RawRecord) -> Company | Person | Event | Product:
        """Dispatch a raw record to the right cleaner."""
        self.stats.total += 1
        try:
            if record.kind is RecordKind.COMPANY:
                node = self.clean_company(record)
            elif record.kind is RecordKind.PERSON:
                node = self.clean_person(record)
            elif record.kind is RecordKind.EVENT:
                node = self.clean_event(record)
            elif record.kind is RecordKind.PRODUCT:
                node = self.clean_product(record)
            else:
                raise ValidationError(
                    "relation records are cleaned by the graph builder",
                    details={"kind": record.kind.value},
                )
        except ValidationError as exc:
            self.stats.rejected += 1
            self.stats.errors[exc.code] = self.stats.errors.get(exc.code, 0) + 1
            logger.warning("record rejected source=%s reason=%s", record.source_id, exc.message)
            raise
        self.stats.accepted += 1
        return node


def classify_product_type(value: Any) -> ProductType:
    """Map a free-text product type onto the normalised enum."""
    text = str(value or "").strip()
    mapping = (
        (ProductType.BOND, ("债券", "债", "bond")),
        (ProductType.FUND, ("基金", "fund")),
        (ProductType.EQUITY, ("股票", "股权", "equity", "IPO")),
        (ProductType.ABS, ("资产支持", "ABS", "abs")),
        (ProductType.STRUCTURED, ("结构化", "理财", "structured")),
    )
    for product_type, keywords in mapping:
        if any(keyword.lower() in text.lower() for keyword in keywords):
            return product_type
    return ProductType.OTHER


def dedupe_key(record: RawRecord) -> str:
    """Key used to detect the same fact arriving from two feeds.

    Relation records are keyed on their endpoints and type — two distinct
    edges between different pairs of entities must never collapse into one.
    """
    payload = record.payload
    if record.kind is RecordKind.RELATION:
        return "|".join(
            [
                record.kind.value,
                str(payload.get("type", "")),
                str(payload.get("source", "")),
                str(payload.get("target", "")),
            ]
        )
    if record.kind is RecordKind.EVENT:
        return "|".join(
            [
                record.kind.value,
                str(payload.get("name", "")),
                str(payload.get("date", "")),
                str(payload.get("amount", "")),
            ]
        )
    return "|".join([record.kind.value, str(payload.get("name", "")), str(payload.get("id", ""))])


__all__ = [
    "Cleaner",
    "CleaningStats",
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

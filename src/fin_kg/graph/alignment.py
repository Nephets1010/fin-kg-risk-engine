"""Entity alignment.

The same legal entity appears under different names across sources
("北京某某科技有限公司" vs "北京某某科技有限责任公司"), so every incoming
record must be resolved to a canonical id before it enters the graph.

The resolver uses a three-stage cascade, from strongest to weakest evidence:

1. **Unified social credit code** — a government-assigned 18-character
   identifier. It is authoritative when present.
2. **Normalised name + region** — the company name is stripped of whitespace,
   parentheses and common suffixes, then matched with the registration
   region. A unique hit is accepted; an ambiguous hit is rejected.
3. **Explicit alias table** — operator-curated mappings that override
   everything else.

A record that matches nothing becomes a *new* entity with a deterministic id
derived from its source and source-specific key, so replaying the same input
twice never creates a duplicate.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from fin_kg.core.errors import ValidationError
from fin_kg.domain.entities import Company, GraphNode

#: Company-name suffixes that carry no distinguishing information.
_NOISE_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "集团有限公司",
    "集团股份有限公司",
    "控股集团有限公司",
    "（集团）",
    "(集团)",
    "分公司",
    "总公司",
)

_WHITESPACE = re.compile(r"\s+")
_PARENTHESIS = re.compile(r"[（()）]")
_PUNCTUATION = re.compile(r"[·、,，.。;；:：\-—_/\\\"'“”‘’]+")

#: Suffixes stripped *after* the noise list, used for the loose key only.
_LOOSE_TAIL = re.compile(r"(集团|公司|企业|实业|控股|投资|科技|有限)$")


def normalize_name(name: str) -> str:
    """Normalise a legal-entity name for matching.

    Full-width characters are folded to half-width, whitespace and brackets
    are removed, and the trailing organisational suffix is dropped.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", name).strip()
    text = _WHITESPACE.sub("", text)
    text = _PARENTHESIS.sub("", text)
    text = _PUNCTUATION.sub("", text)
    for suffix in _NOISE_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


def loose_key(name: str) -> str:
    """A weaker key used only as a last-resort match candidate."""
    base = normalize_name(name)
    return _LOOSE_TAIL.sub("", base)


def deterministic_id(prefix: str, *parts: str) -> str:
    """Build a stable id from the given parts.

    The same inputs always produce the same id, which makes ingestion
    idempotent and replay-safe.
    """
    material = "|".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:20]}"


@dataclass
class ResolutionResult:
    """Outcome of resolving one incoming record."""

    entity_id: str
    is_new: bool
    matched_on: str
    confidence: float
    candidates: list[str] = field(default_factory=list)


class EntityResolver:
    """Resolves incoming records to canonical graph ids.

    The resolver keeps its own index rather than querying the graph on every
    record, which keeps the import of 100k entities a linear operation
    instead of 100k round trips.
    """

    def __init__(self, *, alias_map: dict[str, str] | None = None) -> None:
        self._credit_code_index: dict[str, str] = {}
        self._name_index: dict[str, list[str]] = {}
        self._region_name_index: dict[tuple[str, str], list[str]] = {}
        self._loose_index: dict[str, list[str]] = {}
        self._aliases: dict[str, str] = dict(alias_map or {})

    def register(self, node: GraphNode) -> None:
        """Index an already-known node so later records can match it."""
        credit_code = getattr(node, "credit_code", None)
        if credit_code:
            self._credit_code_index[credit_code] = node.id
        region = getattr(node, "region", None) or ""
        self._index_name(node.id, node.name, region)

    def _index_name(self, entity_id: str, name: str, region: str) -> None:
        normalized = normalize_name(name)
        if not normalized:
            return
        bucket = self._name_index.setdefault(normalized, [])
        if entity_id not in bucket:
            bucket.append(entity_id)
        if region:
            region_bucket = self._region_name_index.setdefault((normalized, region), [])
            if entity_id not in region_bucket:
                region_bucket.append(entity_id)
        loose = loose_key(name)
        if loose:
            loose_bucket = self._loose_index.setdefault(loose, [])
            if entity_id not in loose_bucket:
                loose_bucket.append(entity_id)

    def add_alias(self, alias: str, entity_id: str) -> None:
        """Register an operator-curated alias."""
        self._aliases[normalize_name(alias)] = entity_id

    def resolve_company(
        self,
        *,
        name: str,
        credit_code: str | None = None,
        region: str | None = None,
        source_key: str = "",
    ) -> ResolutionResult:
        """Resolve a company record to a canonical id."""
        if not name:
            raise ValidationError("company name must not be empty")

        if credit_code:
            existing = self._credit_code_index.get(credit_code)
            if existing:
                return ResolutionResult(existing, False, "credit_code", 1.0)

        normalized = normalize_name(name)
        alias_target = self._aliases.get(normalized)
        if alias_target:
            return ResolutionResult(alias_target, False, "alias", 0.95)

        name_candidates = self._name_index.get(normalized, [])

        if region:
            candidates = self._region_name_index.get((normalized, region), [])
            if len(candidates) == 1:
                return ResolutionResult(candidates[0], False, "name_region", 0.9)
            if len(candidates) > 1:
                return ResolutionResult(
                    deterministic_id("company", source_key or name, region),
                    True,
                    "ambiguous_name_region",
                    0.4,
                    candidates,
                )
            # The name is known but not in this region: a same-named company in
            # another region is a different legal entity, so a new one is
            # created rather than merged.
            if name_candidates:
                return ResolutionResult(
                    deterministic_id("company", source_key or name, region),
                    True,
                    "new",
                    1.0,
                    name_candidates,
                )

        if len(name_candidates) == 1:
            return ResolutionResult(name_candidates[0], False, "name", 0.8)
        if len(name_candidates) > 1:
            return ResolutionResult(
                deterministic_id("company", source_key or name),
                True,
                "ambiguous_name",
                0.4,
                name_candidates,
            )

        loose_candidates = self._loose_index.get(loose_key(name), [])
        if len(loose_candidates) == 1:
            return ResolutionResult(loose_candidates[0], False, "loose_name", 0.6)

        entity_id = deterministic_id("company", source_key or name, region or "")
        return ResolutionResult(entity_id, True, "new", 1.0)

    def resolve_generic(self, prefix: str, name: str, source_key: str = "") -> str:
        """Resolve a non-company entity by name, creating an id when unseen."""
        normalized = normalize_name(name)
        candidates = self._name_index.get(normalized, [])
        if len(candidates) == 1:
            return candidates[0]
        return deterministic_id(prefix, source_key or name)

    def make_company(
        self,
        *,
        name: str,
        credit_code: str | None = None,
        region: str | None = None,
        source_key: str = "",
        **extra: object,
    ) -> tuple[Company, ResolutionResult]:
        """Resolve and build a :class:`Company` node in one step."""
        resolution = self.resolve_company(
            name=name, credit_code=credit_code, region=region, source_key=source_key
        )
        company = Company(
            id=resolution.entity_id,
            name=name,
            credit_code=credit_code,
            region=region,
            **extra,
        )
        if resolution.is_new:
            self.register(company)
        return company, resolution


__all__ = [
    "EntityResolver",
    "ResolutionResult",
    "deterministic_id",
    "loose_key",
    "normalize_name",
]

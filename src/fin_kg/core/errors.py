"""Error taxonomy.

The platform distinguishes three families of failure:

* :class:`FinKgError` — the base class for every error the platform raises
  deliberately. Each carries a stable ``code`` used by the API layer and a
  ``http_status`` used by the FastAPI exception handlers.
* :class:`ValidationError` — the caller's input was malformed. Never retried.
* :class:`DependencyError` — an external system (Neo4j, Redis, an LLM vendor)
  failed. Retrying is meaningful for some of these, which is why they carry a
  ``retryable`` flag.

Anything that is *not* one of these is a bug and must propagate with a full
traceback; the logging layer converts them into ``internal_error`` entries.
"""

from __future__ import annotations


class FinKgError(Exception):
    """Base class for all deliberate platform errors."""

    code: str = "fin_kg_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = details or {}

    def to_dict(self) -> dict[str, object]:
        """Serialise to the API error envelope."""
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(FinKgError):
    """Caller supplied invalid input."""

    code = "validation_error"
    http_status = 422
    retryable = False


class NotFoundError(FinKgError):
    """A referenced entity, edge or job does not exist."""

    code = "not_found"
    http_status = 404
    retryable = False


class ConflictError(FinKgError):
    """The request conflicts with the current state of the resource."""

    code = "conflict"
    http_status = 409
    retryable = False


class AuthenticationError(FinKgError):
    """Missing or invalid API credential."""

    code = "unauthorized"
    http_status = 401
    retryable = False


class RateLimitError(FinKgError):
    """The caller exceeded the configured request budget."""

    code = "rate_limited"
    http_status = 429
    retryable = True


class DependencyError(FinKgError):
    """An external dependency failed."""

    code = "dependency_unavailable"
    http_status = 503
    retryable = True


class GraphStoreError(DependencyError):
    """The graph store could not serve the request."""

    code = "graph_store_error"
    http_status = 503


class IngestionError(FinKgError):
    """A source adapter or the ETL pipeline failed."""

    code = "ingestion_error"
    http_status = 500


class RuleEvaluationError(FinKgError):
    """A risk rule could not be evaluated."""

    code = "rule_evaluation_error"
    http_status = 500


class LLMError(DependencyError):
    """The optional LLM backend failed or is misconfigured."""

    code = "llm_error"
    http_status = 503


__all__ = [
    "AuthenticationError",
    "ConflictError",
    "DependencyError",
    "FinKgError",
    "GraphStoreError",
    "IngestionError",
    "LLMError",
    "NotFoundError",
    "RateLimitError",
    "RuleEvaluationError",
    "ValidationError",
]

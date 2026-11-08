"""Structured logging.

Log records are emitted as JSON in production and as a readable console format
in development. The formatter injects a stable set of context keys so that
every line can be correlated with a request, a job or a rule evaluation.

Secret material never reaches the log: :func:`register_secret_filter` masks
registered values, and the settings layer registers every configured secret
automatically at startup.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from fin_kg.core.errors import FinKgError

LOGGER_NAME = "fin_kg"

#: Per-context correlation identifiers carried through the request/job lifecycle.
context_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
context_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)

#: Values that must be masked before they reach a log sink.
_SECRET_VALUES: set[str] = set()

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def register_secret_filter(*values: str | None) -> None:
    """Register string values that must be masked in every log record."""
    for value in values:
        if value and len(value) >= 4:
            _SECRET_VALUES.add(value)


def mask_secrets(text: str) -> str:
    """Replace every registered secret occurrence with ``***``."""
    for secret in _SECRET_VALUES:
        if secret in text:
            text = text.replace(secret, "***")
    return text


class JsonFormatter(logging.Formatter):
    """Render records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = context_request_id.get()
        if request_id:
            payload["request_id"] = request_id
        job_id = context_job_id.get()
        if job_id:
            payload["job_id"] = job_id
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return mask_secrets(json.dumps(payload, ensure_ascii=False, default=str))


class ConsoleFormatter(logging.Formatter):
    """Human readable single-line format used during development."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        request_id = context_request_id.get()
        if request_id:
            extras["request_id"] = request_id
        if extras:
            base += " | " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return mask_secrets(base)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the root handler for the ``fin_kg`` logger tree."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    logger.addHandler(handler)

    # Uvicorn duplicates access logs through its own handlers; point them at us.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        std = logging.getLogger(noisy)
        std.handlers = []
        std.propagate = True


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger inside the ``fin_kg`` namespace."""
    if not name:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith("fin_kg"):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def log_exception(logger: logging.Logger, message: str, exc: BaseException) -> None:
    """Log an exception with the platform's taxonomy applied."""
    if isinstance(exc, FinKgError):
        logger.error(
            "%s code=%s retryable=%s details=%s",
            message,
            exc.code,
            exc.retryable,
            exc.details,
            exc_info=exc,
        )
    else:
        logger.exception("%s (unhandled)", message)


__all__ = [
    "ConsoleFormatter",
    "JsonFormatter",
    "configure_logging",
    "context_job_id",
    "context_request_id",
    "get_logger",
    "log_exception",
    "mask_secrets",
    "register_secret_filter",
]

"""Cross-cutting primitives shared by every layer."""

from fin_kg.core.errors import FinKgError
from fin_kg.core.logging import get_logger

__all__ = ["FinKgError", "get_logger"]

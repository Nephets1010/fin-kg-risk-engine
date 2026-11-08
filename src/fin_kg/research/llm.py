"""Optional LLM client.

The platform treats the LLM as an *optional* accelerator, never as a
dependency. When it is disabled — which is the default — the research module
still answers questions, it just answers them with deterministic templates.

Design rules that keep the LLM from becoming a liability:

* The API key lives in the environment and is never logged. The logging
  filter masks it in every record.
* The LLM never receives raw database credentials, and never generates
  executable Cypher.
* Every LLM call has a timeout and a retry policy; a failure degrades to the
  template answer instead of propagating.
"""

from __future__ import annotations

import json
from typing import Any

from fin_kg.core.errors import LLMError
from fin_kg.core.logging import get_logger, register_secret_filter
from fin_kg.config.settings import LLMSettings

logger = get_logger(__name__)


class LLMClient:
    """A thin, defensive wrapper over an OpenAI-compatible chat endpoint."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        if settings.api_key.get_secret_value():
            register_secret_filter(settings.api_key.get_secret_value())

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled and self.settings.api_key.get_secret_value())

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """Run one completion and return the text."""
        if not self.enabled:
            raise LLMError("the LLM backend is not enabled")

        import httpx

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 1200,
        }
        url = f"{self.settings.api_base.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = httpx.post(
                    url, json=payload, headers=headers, timeout=self.settings.timeout
                )
                response.raise_for_status()
                body = response.json()
                return str(body["choices"][0]["message"]["content"]).strip()
            except Exception as exc:  # noqa: BLE001 - httpx raises several error types
                last_error = exc
                logger.warning("LLM request failed (attempt %s): %s", attempt + 1, type(exc).__name__)
        raise LLMError("LLM request failed", details={"error": str(last_error)})

    def complete_json(self, prompt: str, *, system: str | None = None) -> Any:
        """Run one completion and parse the answer as JSON."""
        text = self.complete(prompt, system=system)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(
                "the model did not return valid JSON", details={"raw": text[:500]}
            ) from exc


def build_client(settings: LLMSettings) -> LLMClient | None:
    """Return a client when the LLM is configured, otherwise ``None``."""
    client = LLMClient(settings)
    if not client.enabled:
        logger.info("LLM backend disabled — the research module will use templates")
        return None
    return client


__all__ = ["LLMClient", "build_client"]

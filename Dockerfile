# ---------------------------------------------------------------------------
# fin-kg-risk-engine — multi-stage build
#
# Stage 1 installs the package and its runtime dependencies into a virtual
# environment; stage 2 copies that environment. The package is installed
# from the full source tree, because setuptools discovers sub-packages by
# looking for their __init__.py — copying only the top-level one would
# produce a wheel with a single module in it and an image with no
# importable package at all.
#
# Only the runtime dependencies go into the image. The development extras
# (pytest, ruff, mypy) are a CI concern and would otherwise double the
# size of the production image.
# ---------------------------------------------------------------------------

# --- Stage 1: package and dependencies --------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --upgrade pip setuptools wheel \
    && pip install .

# --- Stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    FIN_KG_ENV=production

# `curl` is used by the container health check.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY rules/ ./rules/
COPY migrations/ ./migrations/
COPY data/samples/ ./data/samples/
COPY data/aliases.yaml ./data/aliases.yaml

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "fin_kg.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

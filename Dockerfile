# ---------------------------------------------------------------------------
# fin-kg-risk-engine — multi-stage build
#
# Stage 1 installs the dependencies into a virtual environment; stage 2
# copies the source. Keeping them separate means the dependency layer is
# cached and only re-installed when pyproject.toml changes.
# ---------------------------------------------------------------------------

# --- Stage 1: dependencies -------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src/fin_kg/__init__.py src/fin_kg/__init__.py

RUN pip install --upgrade pip setuptools wheel \
    && pip install ".[dev]"

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
COPY src/ ./src/
COPY rules/ ./rules/
COPY migrations/ ./migrations/
COPY data/ ./data/

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000 9100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "fin_kg.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

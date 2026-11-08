"""The FastAPI application.

The app wires the routers, installs the error handlers and the metrics
middleware, and exposes the generated OpenAPI document.

Error handling contract
-----------------------
Every error the platform raises deliberately is returned in the same
envelope::

    {"code": "not_found", "message": "entity not found", "details": {...}}

The HTTP status comes from the error class, so the taxonomy in
:mod:`fin_kg.core.errors` is the single source of truth for both.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fin_kg import __version__
from fin_kg.api.routers import graph, health, jobs, research, risk
from fin_kg.config.settings import Settings, get_settings
from fin_kg.core.errors import FinKgError
from fin_kg.core.logging import (
    configure_logging,
    context_request_id,
    get_logger,
    log_exception,
    register_secret_filter,
)
from fin_kg.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

logger = get_logger(__name__)

DESCRIPTION = """
金融知识图谱风险预警与智能投研系统。

* **图谱检索** —— 实体、路径、邻域与规模统计
* **风险预警** —— 指标读数、风险分、排名、规则预警与风险传导敞口
* **智能投研** —— 自然语言问答、结构可比案例、投研简报
* **任务调度** —— 数据接入与重算的 Init/Step/Poll 长任务接口

所有预警与回答都带有**证据路径**，可回溯到具体的节点与关系。
"""

TAGS_METADATA: list[dict[str, Any]] = [
    {"name": "graph", "description": "知识图谱检索与路径查询"},
    {"name": "risk", "description": "风险评分、预警与传导"},
    {"name": "research", "description": "智能投研：问答、可比案例、简报"},
    {"name": "jobs", "description": "长任务：数据接入与重算"},
    {"name": "ops", "description": "健康检查与监控指标"},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure logging on startup and release resources on shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    register_secret_filter(
        settings.neo4j.password.get_secret_value(),
        settings.redis.password.get_secret_value(),
        settings.api_key.get_secret_value(),
        settings.llm.api_key.get_secret_value(),
    )
    logger.info("fin-kg-risk-engine %s starting in %s mode", __version__, settings.env)
    yield
    logger.info("fin-kg-risk-engine shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    resolved = settings or get_settings()

    app = FastAPI(
        title="fin-kg-risk-engine",
        description=DESCRIPTION,
        version=__version__,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def observe_requests(request: Request, call_next: Any) -> Any:
        """Attach a request id, time the request and record the metrics."""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = context_request_id.set(request_id)
        started = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            context_request_id.reset(token)
            duration = time.monotonic() - started
            path = request.url.path
            HTTP_REQUESTS.labels(method=request.method, path=path, status=str(status_code)).inc()
            HTTP_LATENCY.labels(method=request.method, path=path).observe(duration)

    @app.exception_handler(FinKgError)
    async def handle_domain_error(request: Request, exc: FinKgError) -> JSONResponse:
        """Map every deliberate platform error onto the error envelope."""
        log_exception(logger, "request failed", exc)
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Normalise the HTTP errors raised inside the routers.

        A router that raises ``HTTPException(detail={"code": ..., ...})``
        would otherwise be serialised by FastAPI as ``{"detail": {...}}``,
        while the platform's own errors use the flat envelope declared in
        ``docs/api.md``. Two shapes for one error taxonomy means a client
        needs two parsers, so the flat envelope wins everywhere.
        """
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            content = {
                "code": str(detail.get("code", "http_error")),
                "message": str(detail.get("message", "")),
            }
            if detail.get("details"):
                content["details"] = detail["details"]
            return JSONResponse(status_code=exc.status_code, content=content)
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": "http_error", "message": str(detail)},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Normalise FastAPI's own request validation errors.

        FastAPI reports a malformed body or query parameter as
        ``{"detail": [...]}`` — a third shape, on the most common error path
        of all. The platform's envelope is flat, so the validation errors are
        flattened onto it too. The raw error list is preserved under
        ``details`` so that nothing is lost.
        """
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = ".".join(str(part) for part in first.get("loc", []) if part != "body")
        message = first.get("msg", "request validation failed")
        if location:
            message = f"{location}: {message}"
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": message,
                "details": {"errors": errors},
            },
        )

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
        """Map an unhandled ``ValueError`` onto a 422."""
        logger.warning("invalid request on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=422,
            content={"code": "validation_error", "message": str(exc)},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all handler — an unexpected error is a bug, not a contract."""
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "an unexpected error occurred",
            },
        )

    app.include_router(health.router)
    app.include_router(graph.router)
    app.include_router(risk.router)
    app.include_router(research.router)
    app.include_router(jobs.router)

    @app.get("/", tags=["ops"], summary="服务信息")
    def root() -> dict[str, Any]:
        """Return the service banner."""
        return {
            "name": "fin-kg-risk-engine",
            "version": __version__,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "metrics": "/metrics",
        }

    return app


app = create_app()

__all__ = ["app", "create_app"]

"""Research endpoints: Q&A, similar cases and report generation."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from fin_kg.api.deps import KGQADep, ReportDep, RiskServiceDep, require_api_key
from fin_kg.api.schemas import (
    AnswerResponse,
    AskRequest,
    ErrorResponse,
    EvidenceResponse,
    ReportResponse,
    ReportSectionResponse,
    SimilarCaseResponse,
)
from fin_kg.core.errors import ValidationError
from fin_kg.research.similarity import SimilarCaseFinder

router = APIRouter(
    prefix="/api/v1/research",
    tags=["research"],
    dependencies=[Depends(require_api_key)],
)

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "API Key 缺失或错误"},
    404: {"model": ErrorResponse, "description": "实体或问题目标不存在"},
    422: {"model": ErrorResponse, "description": "参数不合法"},
}


@router.post(
    "/ask",
    response_model=AnswerResponse,
    summary="自然语言图谱问答",
    responses=ERROR_RESPONSES,
)
def ask(request: AskRequest, kgqa: KGQADep) -> AnswerResponse:
    """Answer a natural-language question about the graph.

    The response always carries the evidence path, so an analyst can verify
    every claim against the graph.
    """
    try:
        answer = kgqa.ask(request.question)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    return AnswerResponse(
        question=answer.question,
        answer=answer.answer,
        intent=answer.intent.value,
        confidence=answer.confidence,
        evidence=[
            EvidenceResponse(
                kind=item.kind,
                description=item.description,
                nodeIds=item.node_ids,
                nodeNames=item.node_names,
                relationTypes=item.relation_types,
            )
            for item in answer.evidence
        ],
        data=answer.data,
        plan=answer.plan,
    )


@router.get(
    "/similar/{entity_id}",
    response_model=list[SimilarCaseResponse],
    summary="查找结构可比案例",
    responses=ERROR_RESPONSES,
)
def similar_cases(
    entity_id: str,
    service: RiskServiceDep,
    top_k: int = Query(default=10, ge=1, le=50),
    min_similarity: float = Query(default=0.3, ge=0.0, le=1.0),
) -> list[SimilarCaseResponse]:
    """Return the companies that are structurally similar to an entity."""
    service.score(entity_id)  # raises 404 when the entity does not exist
    from fin_kg.api.deps import get_repository

    finder = SimilarCaseFinder(get_repository())
    cases = finder.find(entity_id, top_k=top_k, min_similarity=min_similarity)
    return [
        SimilarCaseResponse(
            entityId=case.entity_id,
            entityName=case.entity_name,
            similarity=case.similarity,
            dimensions=case.dimensions,
            riskScore=case.risk_score,
            riskLevel=case.risk_level,
            reason=case.reason,
        )
        for case in cases
    ]


@router.get(
    "/report/{entity_id}",
    response_model=ReportResponse,
    summary="生成投研简报",
    responses=ERROR_RESPONSES,
)
def research_report(entity_id: str, service: RiskServiceDep, report: ReportDep) -> ReportResponse:
    """Generate a research briefing for an entity."""
    assessment = service.assess(entity_id)
    document = report.generate(assessment)
    return ReportResponse(
        entityId=document.entity_id,
        entityName=document.entity_name,
        generatedAt=document.generated_at,
        markdown=document.to_markdown(),
        sections=[
            ReportSectionResponse(
                title=section.title,
                content=section.content,
                bullets=section.bullets,
                data=section.data,
            )
            for section in document.sections
        ],
    )


@router.get(
    "/report/{entity_id}/download",
    response_class=Response,
    summary="下载投研简报 Markdown",
    responses=ERROR_RESPONSES,
)
def research_report_download(
    entity_id: str, service: RiskServiceDep, report: ReportDep
) -> Response:
    """Serve the same briefing as ``/report`` as a Markdown attachment.

    The body is byte-for-byte the ``markdown`` of the normal report. The
    filename is RFC 5987 encoded so a Chinese entity name survives the
    header; the ASCII entity id is kept as a compatibility fallback.
    """
    assessment = service.assess(entity_id)
    document = report.generate(assessment)
    body = document.to_markdown()
    raw_name = (document.entity_name or entity_id).strip()
    safe_name = raw_name.replace("/", "_").replace("\\", "_")
    safe_name = "".join(char for char in safe_name if ord(char) >= 32) or entity_id
    quoted = quote(f"{safe_name}.md", safe="")
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{entity_id}.md\"; filename*=UTF-8''{quoted}"
            )
        },
    )


__all__ = ["router"]

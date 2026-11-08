"""Research endpoints: Q&A, similar cases and report generation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from fin_kg.api.deps import KGQADep, ReportDep, RiskServiceDep
from fin_kg.api.schemas import (
    AnswerResponse,
    ErrorResponse,
    EvidenceResponse,
    ReportResponse,
    ReportSectionResponse,
    SimilarCaseResponse,
)
from fin_kg.research.similarity import SimilarCaseFinder

router = APIRouter(prefix="/api/v1/research", tags=["research"])

ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "实体或问题目标不存在"},
    422: {"model": ErrorResponse, "description": "参数不合法"},
}


@router.post(
    "/ask",
    response_model=AnswerResponse,
    summary="自然语言图谱问答",
    responses=ERROR_RESPONSES,
)
def ask(question: dict, kgqa: KGQADep) -> AnswerResponse:
    """Answer a natural-language question about the graph.

    The response always carries the evidence path, so an analyst can verify
    every claim against the graph.
    """
    try:
        answer = kgqa.ask(str(question.get("question", "")))
    except Exception as exc:  # noqa: BLE001 - mapped to a 422 by the handler
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
    cases = finder.find(
        entity_id, top_k=top_k, min_similarity=min_similarity
    )
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


__all__ = ["router"]

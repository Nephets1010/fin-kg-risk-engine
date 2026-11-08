"""Risk endpoints: scores, rankings, alerts and exposure."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from fin_kg.api.deps import RiskServiceDep, require_api_key
from fin_kg.api.schemas import (
    AlertListResponse,
    AlertResponse,
    ErrorResponse,
    ExposureResponse,
    IndicatorReadingResponse,
    PathResponse,
    RiskRankResponse,
    RiskScoreResponse,
)
from fin_kg.domain.risk import RiskAlert, RiskScore, TransmissionPath

router = APIRouter(
    prefix="/api/v1/risk",
    tags=["risk"],
    dependencies=[Depends(require_api_key)],
)

ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "API Key 缺失或错误"},
    404: {"model": ErrorResponse, "description": "实体不存在"},
    422: {"model": ErrorResponse, "description": "参数不合法"},
}


def _score_response(score: RiskScore) -> RiskScoreResponse:
    return RiskScoreResponse(
        entityId=score.entity_id,
        entityName=score.entity_name,
        entityType=score.entity_type,
        score=score.score,
        level=score.level,
        modelVersion=score.model_version,
        computedAt=score.computed_at,
        readings=[
            IndicatorReadingResponse(
                indicatorId=reading.indicator_id,
                indicatorName=reading.indicator_name,
                version=reading.version,
                category=reading.category,
                measurement=reading.measurement,
                subScore=reading.sub_score,
                weight=reading.weight,
                unit=reading.unit,
                detail=reading.detail,
            )
            for reading in score.readings
        ],
        explanation=score.explain(),
        modelScore=score.model_score,
        scoreGap=score.score_gap,
    )


def _path_response(path: TransmissionPath) -> PathResponse:
    return PathResponse(
        nodeIds=list(path.node_ids),
        nodeNames=list(path.node_names),
        relationTypes=list(path.relation_types),
        hops=path.hops,
        propagatedScore=path.propagated_score,
        text=path.to_text(),
    )


def _alert_response(alert: RiskAlert) -> AlertResponse:
    return AlertResponse(
        alertId=alert.alert_id,
        ruleId=alert.rule_id,
        ruleName=alert.rule_name,
        severity=alert.severity.value,
        status=alert.status.value,
        entityId=alert.entity_id,
        entityName=alert.entity_name,
        score=alert.score,
        message=alert.message,
        evidence=alert.evidence,
        transmissionPaths=[_path_response(path) for path in alert.transmission_paths],
        triggeredAt=alert.triggered_at,
    )


@router.get(
    "/entities/{entity_id}/score",
    response_model=RiskScoreResponse,
    summary="查询实体风险分",
    responses=ERROR_RESPONSES,
)
def entity_score(entity_id: str, service: RiskServiceDep) -> RiskScoreResponse:
    """Compute the risk score of one entity."""
    score = service.score(entity_id)
    return _score_response(score)


@router.get(
    "/rank",
    response_model=RiskRankResponse,
    summary="风险分排名",
    responses=ERROR_RESPONSES,
)
def risk_rank(
    service: RiskServiceDep,
    limit: int = Query(default=20, ge=1, le=200),
    candidates: int | None = Query(
        default=None,
        ge=1,
        le=100_000,
        description=(
            "只对风险分最高的 N 个候选重新评分。留空则全量评分（结果最准，但耗时随图规模线性增长）"
        ),
    ),
) -> RiskRankResponse:
    """Return the highest-risk entities."""
    scores = service.rank(limit=limit, candidates=candidates)
    return RiskRankResponse(items=[_score_response(score) for score in scores], total=len(scores))


@router.get(
    "/alerts",
    response_model=AlertListResponse,
    summary="风险预警列表",
    responses=ERROR_RESPONSES,
)
def list_alerts(
    service: RiskServiceDep,
    min_severity: str = Query(default="info", description="info | warning | major | critical"),
    limit: int = Query(default=50, ge=1, le=500),
) -> AlertListResponse:
    """Evaluate the rules over the graph and return the alerts."""
    if min_severity not in {"info", "warning", "major", "critical"}:
        raise HTTPException(
            status_code=422,
            detail={"code": "validation_error", "message": "invalid min_severity"},
        )
    alerts = service.alerts(min_severity=min_severity, limit=limit)
    counts: dict[str, int] = {}
    for alert in alerts:
        counts[alert.severity.value] = counts.get(alert.severity.value, 0) + 1
    return AlertListResponse(
        items=[_alert_response(alert) for alert in alerts],
        total=len(alerts),
        severities=counts,
    )


@router.get(
    "/entities/{entity_id}/alerts",
    response_model=list[AlertResponse],
    summary="查询实体触发的预警",
    responses=ERROR_RESPONSES,
)
def entity_alerts(entity_id: str, service: RiskServiceDep) -> list[AlertResponse]:
    """Return the alerts raised for one entity."""
    assessment = service.assess(entity_id)
    return [_alert_response(alert) for alert in assessment.alerts]


@router.get(
    "/entities/{entity_id}/exposure",
    response_model=ExposureResponse,
    summary="查询风险敞口",
    responses=ERROR_RESPONSES,
)
def entity_exposure(
    entity_id: str,
    service: RiskServiceDep,
    hops: int = Query(default=3, ge=1, le=6),
) -> ExposureResponse:
    """Return the entities exposed to one entity's risk."""
    score = service.score(entity_id)
    affected = service.propagate(score)
    return ExposureResponse(
        entityId=score.entity_id,
        entityName=score.entity_name,
        score=score.score,
        affected=affected,
    )


__all__ = ["router"]

"""Graph endpoints: entities, statistics, paths and neighbourhoods."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from fin_kg.api.deps import Repository, require_api_key
from fin_kg.api.schemas import (
    EntityListResponse,
    EntityResponse,
    ErrorResponse,
    GraphStatsResponse,
    PathResponse,
)
from fin_kg.domain.relations import RelationType

router = APIRouter(
    prefix="/api/v1/graph",
    tags=["graph"],
    dependencies=[Depends(require_api_key)],
)

ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "API Key 缺失或错误"},
    404: {"model": ErrorResponse, "description": "实体不存在"},
    422: {"model": ErrorResponse, "description": "参数不合法"},
}


@router.get("/stats", response_model=GraphStatsResponse, summary="图谱规模统计")
def graph_stats(repository: Repository) -> GraphStatsResponse:
    """Return the node and relationship counts of the graph."""
    stats = repository.stats()
    total_relationships = int(stats.get("Relationship", 0))
    relationships = {
        key.removeprefix("REL:"): int(value)
        for key, value in stats.items()
        if key.startswith("REL:")
    }
    nodes = {key: int(value) for key, value in stats.items() if not key.startswith("REL:")}
    nodes.pop("Relationship", None)
    return GraphStatsResponse(
        nodes=nodes,
        relationships=relationships,
        totalNodes=sum(nodes.values()),
        totalRelationships=total_relationships,
    )


@router.get(
    "/entities",
    response_model=EntityListResponse,
    summary="分页查询实体",
    responses=ERROR_RESPONSES,
)
def list_entities(
    repository: Repository,
    label: str = Query(default="Company", description="节点标签"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> EntityListResponse:
    """List the entities of one label."""
    nodes = repository.find_nodes(label)
    page = nodes[offset : offset + limit]
    return EntityListResponse(
        items=[
            EntityResponse(
                id=str(node.get("id", "")),
                name=str(node.get("name", "")),
                labels=[label],
                properties=node,
            )
            for node in page
        ],
        total=len(nodes),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/entities/{entity_id}",
    response_model=EntityResponse,
    summary="查询单个实体",
    responses=ERROR_RESPONSES,
)
def get_entity(entity_id: str, repository: Repository) -> EntityResponse:
    """Return one entity and all of its properties."""
    node = repository.get_node(entity_id)
    if node is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "entity not found"},
        )
    return EntityResponse(
        id=str(node.get("id", entity_id)),
        name=str(node.get("name", "")),
        labels=["Entity"],
        properties=node,
    )


@router.get(
    "/entities/{entity_id}/neighbourhood",
    response_model=list[PathResponse],
    summary="查询 k 跳邻域路径",
    responses=ERROR_RESPONSES,
)
def neighbourhood(
    entity_id: str,
    repository: Repository,
    hops: int = Query(default=2, ge=1, le=6),
    relations: str = Query(
        default="HOLD,GUARANTEE,RELATED,CONTROLS,INVESTS_IN",
        description="逗号分隔的关系类型",
    ),
) -> list[PathResponse]:
    """Return the k-hop paths that start at the entity."""
    wanted = _parse_relations(relations)
    paths = repository.traverse(entity_id, wanted, max_hops=hops, direction="out")
    return [
        PathResponse(
            nodeIds=path.node_ids(),
            nodeNames=[str(node.get("name", "")) for node in path.nodes],
            relationTypes=path.relation_types(),
            hops=path.length,
            text=" -> ".join(str(node.get("name", "")) for node in path.nodes),
        )
        for path in paths
    ]


@router.get(
    "/entities/{entity_id}/paths",
    response_model=list[PathResponse],
    summary="查询两实体之间的最短路径",
    responses=ERROR_RESPONSES,
)
def paths_between(
    repository: Repository,
    source: str = Query(description="起点实体 ID"),
    target: str = Query(description="终点实体 ID"),
    hops: int = Query(default=4, ge=1, le=8),
) -> list[PathResponse]:
    """Return the shortest paths between two entities."""
    if repository.get_node(source) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"source entity {source} not found"},
        )
    if repository.get_node(target) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"target entity {target} not found"},
        )
    paths = repository.traverse(
        source,
        list(RelationType),
        max_hops=hops,
        direction="both",
    )
    results: list[PathResponse] = []
    for path in paths:
        if target not in path.node_ids():
            continue
        results.append(
            PathResponse(
                nodeIds=path.node_ids(),
                nodeNames=[str(node.get("name", "")) for node in path.nodes],
                relationTypes=path.relation_types(),
                hops=path.length,
                text=" -> ".join(str(node.get("name", "")) for node in path.nodes),
            )
        )
    return results


def _parse_relations(raw: str) -> list[RelationType]:
    """Parse a comma-separated relation list, ignoring unknown names."""
    result: list[RelationType] = []
    for item in raw.split(","):
        name = item.strip().upper()
        if not name:
            continue
        try:
            result.append(RelationType(name))
        except ValueError:
            continue
    return result


__all__ = ["router"]

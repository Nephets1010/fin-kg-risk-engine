"""Cypher query library.

All multi-hop and aggregation queries used by the risk engine and the
research module live here as named constants. Keeping them in one place
means the query surface is auditable: a reviewer can see every read the
platform performs against the graph without grepping the codebase.

Every statement is parameterised — no user input is ever concatenated into
Cypher.
"""

from __future__ import annotations

#: Nodes of a company ordered by risk score, optionally filtered.
TOP_RISKED_COMPANIES = """
MATCH (c:Company)
WHERE $minScore IS NULL OR c.riskScore >= $minScore
RETURN c{.*} AS company
ORDER BY c.riskScore DESC
LIMIT $limit
"""

#: The k-hop neighbourhood of an entity, used for subgraph sampling and GNN.
K_HOP_NEIGHBOURHOOD = """
MATCH (s:Entity {id: $id})
CALL (s) {
    WITH s
    MATCH p = (s)-[*1..$hops]-(t)
    WHERE all(r IN relationships(p) WHERE r.relationType IN $types)
    RETURN collect(DISTINCT t) AS reachable
}
UNWIND reachable AS t
RETURN t{.*} AS node
LIMIT $limit
"""

#: Shareholding paths between two companies.
HOLDING_PATHS = """
MATCH p = (a:Company {id: $source})-[:HOLD*1..$hops]->(b:Company {id: $target})
RETURN [n IN nodes(p) | n.id] AS nodeIds,
       [n IN nodes(p) | n.name] AS nodeNames,
       [r IN relationships(p) | r.percent] AS percents
"""

#: Guarantee chain from a company — the flagship "担保链穿透" query.
GUARANTEE_CHAIN = """
MATCH p = (c:Company {id: $id})-[:GUARANTEE*1..$hops]->(t)
WHERE NOT t:Company OR true
RETURN [n IN nodes(p) | n{.*}] AS nodes,
       [r IN relationships(p) | r{.*}] AS relations,
       length(p) AS hops
ORDER BY hops DESC
"""

#: Events of a company inside a time window.
COMPANY_EVENTS = """
MATCH (c:Entity {id: $id})-[:INVOLVED_IN]->(e:Event)
WHERE $since IS NULL OR e.occurredDate >= $since
RETURN e{.*} AS event
ORDER BY e.occurredDate DESC
LIMIT $limit
"""

#: Aggregate event counts per company over a window — feeds the indicators.
EVENT_AGGREGATE = """
MATCH (c:Company)-[:INVOLVED_IN]->(e:Event)
WHERE $since IS NULL OR e.occurredDate >= $since
RETURN c.id AS companyId,
       count(e) AS total,
       sum(CASE WHEN e.eventType = 'litigation' THEN 1 ELSE 0 END) AS litigation,
       sum(CASE WHEN e.eventType = 'enforcement' THEN 1 ELSE 0 END) AS enforcement,
       sum(CASE WHEN e.eventType = 'penalty' THEN 1 ELSE 0 END) AS penalty,
       sum(CASE WHEN e.eventType = 'rating_downgrade' THEN 1 ELSE 0 END) AS downgrade,
       sum(CASE WHEN e.eventType = 'negative_news' THEN 1 ELSE 0 END) AS negativeNews,
       sum(CASE WHEN e.eventType = 'default' THEN 1 ELSE 0 END) AS default,
       sum(coalesce(e.amount, 0.0)) AS amountSum
"""

#: Outbound guarantee exposure per company.
GUARANTEE_EXPOSURE = """
MATCH (c:Company)-[g:GUARANTEE]->(t:Company)
RETURN c.id AS companyId,
       count(g) AS guaranteeCount,
       sum(coalesce(g.guaranteed_amount, 0.0)) AS guaranteeAmount,
       collect(DISTINCT t.id) AS guaranteedParties
"""

#: Largest shareholder per company.
TOP_SHAREHOLDER = """
MATCH (c:Company)<-[h:HOLD]-(s:Company)
WITH c, s, h ORDER BY h.percent DESC
WITH c, collect({holder: s{.*}, percent: h.percent})[0] AS top
RETURN c.id AS companyId, top.holder AS holder, top.percent AS percent
"""

#: People legally representing a company, with their own risk score.
LEGAL_REPRESENTATIVES = """
MATCH (p:Person)-[:LEGAL_REP]->(c:Company)
RETURN c.id AS companyId, collect({person: p{.*}, riskScore: p.riskScore}) AS representatives
"""

#: Companies that a given company guarantees, plus downstream exposure.
GUARANTEED_BY = """
MATCH (c:Company)<-[:GUARANTEE]-(g:Company)
RETURN c.id AS companyId,
       collect({guarantor: g{.*}, amount: g.guaranteed_amount}) AS guarantors
"""

#: Products issued by a company.
ISSUED_PRODUCTS = """
MATCH (c:Company)-[:ISSUE]->(p:Product)
RETURN c.id AS companyId, collect(p{.*}) AS products
"""

#: Search companies and persons by a fuzzy name fragment.
SEARCH_BY_NAME = """
CALL (n:Entity) {
    WHERE n.name CONTAINS $query
    RETURN n
}
RETURN n{.*} AS node
LIMIT $limit
"""

#: Neighbourhood used for the "who is exposed to this company" view.
EXPOSURE_CIRCLE = """
MATCH (c:Company {id: $id})-[r:GUARANTEE|HOLD]->(t:Company)
WITH c, collect({name: t.name, id: t.id, type: r.relationType, weight: r.transmissibility}) AS direct
RETURN direct
"""

#: Path search between two arbitrary entities for the research console.
PATH_SEARCH = """
MATCH p = shortestPath((a:Entity {id: $source})-[*1..$hops]-(b:Entity {id: $target}))
RETURN [n IN nodes(p) | n{.*}] AS nodes,
       [r IN relationships(p) | r{.*}] AS relations,
       length(p) AS hops
"""

#: Graph-wide counters used by the dashboard.
GRAPH_COUNTERS = """
CALL (n:Entity) { RETURN count(n) AS nodeCount }
CALL ()-[r]->() { RETURN count(r) AS relCount }
RETURN nodeCount, relCount
"""

__all__ = [
    "COMPANY_EVENTS",
    "EVENT_AGGREGATE",
    "EXPOSURE_CIRCLE",
    "GRAPH_COUNTERS",
    "GUARANTEED_BY",
    "GUARANTEE_CHAIN",
    "GUARANTEE_EXPOSURE",
    "HOLDING_PATHS",
    "ISSUED_PRODUCTS",
    "K_HOP_NEIGHBOURHOOD",
    "LEGAL_REPRESENTATIVES",
    "PATH_SEARCH",
    "SEARCH_BY_NAME",
    "TOP_RISKED_COMPANIES",
    "TOP_SHAREHOLDER",
]

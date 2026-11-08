"""Research layer: KGQA, similar-case retrieval, report generation, LLM client."""

from fin_kg.research.kgqa import Answer, Evidence, KGQA
from fin_kg.research.llm import LLMClient, build_client
from fin_kg.research.planner import (
    QueryIntent,
    QueryPlan,
    QueryPlanner,
    classify_intent,
    extract_entity_name,
)
from fin_kg.research.report import ReportGenerator, ReportSection, ResearchReport
from fin_kg.research.similarity import (
    SIMILARITY_DIMENSIONS,
    SimilarCase,
    SimilarCaseFinder,
    cosine_similarity,
)

__all__ = [
    "SIMILARITY_DIMENSIONS",
    "Answer",
    "Evidence",
    "KGQA",
    "LLMClient",
    "QueryIntent",
    "QueryPlan",
    "QueryPlanner",
    "ReportGenerator",
    "ReportSection",
    "ResearchReport",
    "SimilarCase",
    "SimilarCaseFinder",
    "build_client",
    "classify_intent",
    "cosine_similarity",
    "extract_entity_name",
]

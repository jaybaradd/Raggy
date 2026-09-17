"""Conservative query decomposition for compound document retrieval."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from config import settings


class QueryPlan(BaseModel):
    """The only decomposition shape accepted from the model."""

    model_config = ConfigDict(extra="forbid")
    subqueries: list[str] = Field(min_length=2, max_length=3)


def should_decompose(query: str) -> bool:
    """Avoid an LLM call unless the question has clear independent clauses."""
    lowered = query.casefold()
    markers = (" compare ", " versus ", " vs. ", " and then ", " followed by ")
    return len(query) >= 80 and (any(marker in lowered for marker in markers) or query.count("?") > 1)


async def decompose_query(query: str, provider: Any) -> list[str]:
    """Return at most three retrieval queries, falling back safely to *query*."""
    if not settings.query_decomposition_enabled or not should_decompose(query):
        return [query]
    try:
        raw = await provider.generate_json(_prompt(query), schema=QueryPlan.model_json_schema())
        plan = QueryPlan.model_validate(raw)
        subqueries = _validated_subqueries(plan.subqueries, query)
        return subqueries if len(subqueries) >= 2 else [query]
    except Exception:
        return [query]


def _validated_subqueries(subqueries: list[str], original_query: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for subquery in subqueries[:settings.query_decomposition_max_subqueries]:
        cleaned = " ".join(subquery.split())
        normalized = cleaned.casefold()
        if len(cleaned) < 8 or normalized in seen or normalized == original_query.casefold():
            continue
        seen.add(normalized)
        result.append(cleaned)
    return result


def _prompt(query: str) -> str:
    return f"""Split this compound retrieval question into two or three independent,
searchable document queries. Preserve names, identifiers, dates, and constraints.
Do not answer the question, add facts, or create a plan. Return JSON only:
{{"subqueries": ["first retrieval query", "second retrieval query"]}}

Question:
{query}
"""

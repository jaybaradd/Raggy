"""Select durable memories that are useful for the *current* chat turn.

Exact identifier matches are deliberately treated differently from semantic
matches: they are safe fallback context, while semantic candidates must be
approved by the constrained planner before they reach the answer model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.memory.models import IdentifierReference, MemoryRecord
from core.memory.projection import memory_text
from core.retrieval.memory_scope import is_memory_record_eligible
from config import settings

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*\b")
_EXACT_FALLBACK_CONFIDENCE = 0.80
_EXACT_CANDIDATE_LIMIT = 5
_SEMANTIC_CANDIDATE_LIMIT = 5


class MemoryRelationshipHint(BaseModel):
    """The planner's declared relationship between this turn and one memory."""

    model_config = ConfigDict(extra="forbid")
    memory_id: str = Field(min_length=1)
    relation: Literal["supports", "updates", "duplicates", "related", "uncertain"]
    confidence: float = Field(ge=0.0, le=1.0)


class MemoryPlan(BaseModel):
    """Strict, bounded output accepted from the memory planning model."""

    model_config = ConfigDict(extra="forbid")
    selected_memory_ids: list[str] = Field(default_factory=list, max_length=10)
    relationships: list[MemoryRelationshipHint] = Field(default_factory=list, max_length=10)
    rationale: str | None = Field(default=None, max_length=1_000)


@dataclass(frozen=True)
class MemoryContextResult:
    """Context and provenance returned to the message route."""

    context: str
    memories: list[dict]
    planner_status: Literal["selected", "no_selection", "exact_fallback", "unavailable"]
    rationale: str | None = None
    reconciliation_hints: list[dict] | None = None
    graph_status: Literal["disabled", "no_seeds", "no_candidates", "expanded", "unavailable"] = "disabled"
    candidate_counts: dict[str, int] | None = None


@dataclass(frozen=True)
class _Candidate:
    record: MemoryRecord
    source: Literal["exact_identifier", "semantic", "graph_related"]
    score: float | None = None
    graph_seed_memory_id: str | None = None
    graph_relationship_id: str | None = None
    graph_relationship_type: str | None = None


def extract_identifier_references(text: str) -> list[IdentifierReference]:
    """Extract conservative, domain-neutral identifier-like tokens.

    This is only a candidate-key heuristic (letters plus digits, or long
    numeric references), not an event taxonomy. The planner can still reject
    an unrelated exact-looking candidate.
    """
    references: list[IdentifierReference] = []
    seen: set[str] = set()
    for match in _TOKEN_PATTERN.finditer(text):
        value = match.group(0)
        has_letters = any(character.isalpha() for character in value)
        has_digits = any(character.isdigit() for character in value)
        # Mixed alphanumeric tokens cover tickets, SKUs, invoice references,
        # and similar identifiers. Long digit strings cover numeric references
        # while avoiding everyday values such as "2" or "42".
        if not ((has_letters and has_digits) or (value.isdigit() and len(value) >= 3)):
            continue
        reference = IdentifierReference(value=value, mention=value, confidence=0.90)
        if reference.normalized_value in seen:
            continue
        seen.add(reference.normalized_value)
        references.append(reference)
    return references


def _candidate_payload(candidate: _Candidate) -> dict:
    record = candidate.record
    return {
        "memory_id": record.memory_id,
        "memory_text": memory_text(record),
        "kind": record.kind,
        "scope": record.scope,
        "confidence": record.confidence,
        "score": candidate.score,
        "candidate_source": candidate.source,
        "graph_seed_memory_id": candidate.graph_seed_memory_id,
        "graph_relationship_id": candidate.graph_relationship_id,
        "graph_relationship_type": candidate.graph_relationship_type,
    }


def _render_selected(candidates: list[_Candidate], plan: MemoryPlan | None,
                     *, fallback: bool = False) -> MemoryContextResult:
    by_id = {candidate.record.memory_id: candidate for candidate in candidates}
    if fallback:
        selected_ids = [
            candidate.record.memory_id for candidate in candidates
            if candidate.source == "exact_identifier" and candidate.record.confidence >= _EXACT_FALLBACK_CONFIDENCE
        ]
        status: Literal["selected", "no_selection", "exact_fallback", "unavailable"] = "exact_fallback" if selected_ids else "unavailable"
        relationship_by_id: dict[str, MemoryRelationshipHint] = {}
        rationale = "Planner unavailable; injected high-confidence exact identifier matches only." if selected_ids else None
    else:
        assert plan is not None
        selected_ids = plan.selected_memory_ids
        status = "selected" if selected_ids else "no_selection"
        relationship_by_id = {hint.memory_id: hint for hint in plan.relationships}
        rationale = plan.rationale

    memories: list[dict] = []
    parts: list[str] = []
    for index, memory_id in enumerate(selected_ids, start=1):
        candidate = by_id[memory_id]
        item = _candidate_payload(candidate)
        item["prompt_label"] = f"M{index}"
        item["selection_source"] = "exact_fallback" if fallback else "planner"
        hint = relationship_by_id.get(memory_id)
        item["selection_relation"] = hint.relation if hint else ("related" if not fallback else "supports")
        item["selection_confidence"] = hint.confidence if hint else candidate.record.confidence
        memories.append(item)
        parts.append(f"[{item['prompt_label']} | {item['kind']} | {item['scope']}]\n{item['memory_text']}")
    hints = [
        {
            "memory_id": item["memory_id"],
            "candidate_source": item["candidate_source"],
            "selection_relation": item["selection_relation"],
            "selection_confidence": item["selection_confidence"],
        }
        for item in memories
    ]
    return MemoryContextResult("\n\n".join(parts), memories, status, rationale, hints)


def _validate_plan(plan: MemoryPlan, candidates: list[_Candidate]) -> None:
    candidate_ids = {candidate.record.memory_id for candidate in candidates}
    selected_ids = plan.selected_memory_ids
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("memory plan selected a memory more than once")
    if not set(selected_ids).issubset(candidate_ids):
        raise ValueError("memory plan selected a memory outside the candidate set")
    for hint in plan.relationships:
        if hint.memory_id not in candidate_ids or hint.memory_id not in selected_ids:
            raise ValueError("memory plan relationship does not describe a selected candidate")


def _planning_prompt(query: str, candidates: list[_Candidate]) -> str:
    candidate_lines = []
    for candidate in candidates:
        record = candidate.record
        candidate_lines.append(
            f"- id={record.memory_id}; source={candidate.source}; scope={record.scope}; "
            f"confidence={record.confidence:.2f}; graph_relation={candidate.graph_relationship_type}; "
            f"connected_to={candidate.graph_seed_memory_id}; text={memory_text(record)}"
        )
    return f"""You are selecting durable memory context for one user turn.
Select only candidates that directly help answer the current turn. Do not treat a
candidate as ground truth when the user is correcting it; label that as `updates`.
Semantic candidates are suggestions, not facts. Return no selection when none is
relevant. You may select only the supplied IDs.

Current user turn:
{query}

Candidates:
{chr(10).join(candidate_lines)}

Return JSON only, with exactly these fields:
{{"selected_memory_ids": ["candidate-id"], "relationships": [{{"memory_id": "candidate-id", "relation": "updates", "confidence": 0.9}}], "rationale": "brief reason"}}
"""


async def build_memory_context(
    *,
    query: str,
    owner_id: str,
    session_id: str,
    project_id: str | None,
    project_scope: str | None,
    store: Any | None = None,
    planner_provider: Any | None = None,
    semantic_retriever: Callable[..., Any] | None = None,
    graph: Any | None = None,
    graph_expansion_enabled: bool | None = None,
    graph_expansion_limit: int | None = None,
) -> MemoryContextResult:
    """Build bounded, provenance-rich memory context before answering a turn."""
    if store is None:
        from db.repository_factory import repositories
        store = repositories.memories
    if graph is None:
        from db.repository_factory import repositories
        graph = repositories.graph
    if planner_provider is None:
        from core.llm.gemini import gemini_provider
        planner_provider = gemini_provider
    if semantic_retriever is None:
        from core.retrieval.memory import retrieve_memories
        semantic_retriever = retrieve_memories

    references = extract_identifier_references(query)
    exact_records = store.find_memory_candidates(
        owner_id=owner_id, session_id=session_id, project_id=project_id,
        project_scope=project_scope, identifier_references=references,
        limit=_EXACT_CANDIDATE_LIMIT,
    ) if references else []
    candidates = [_Candidate(record, "exact_identifier") for record in exact_records]
    candidate_ids = {candidate.record.memory_id for candidate in candidates}

    try:
        semantic_result = semantic_retriever(
            query, owner_id=owner_id, session_id=session_id, project_id=project_id,
            project_scope=project_scope, top_k=_SEMANTIC_CANDIDATE_LIMIT,
        )
        for hit in semantic_result.memories:
            memory_id = str(hit.get("memory_id", ""))
            if not memory_id or memory_id in candidate_ids:
                continue
            record = store.get_owned(memory_id, owner_id=owner_id)
            if record is None or not is_memory_record_eligible(
                record, owner_id=owner_id, session_id=session_id,
                project_id=project_id, project_scope=project_scope,
            ):
                continue
            candidates.append(_Candidate(record, "semantic", hit.get("score")))
            candidate_ids.add(memory_id)
            if len(candidates) >= _EXACT_CANDIDATE_LIMIT + _SEMANTIC_CANDIDATE_LIMIT:
                break
    except Exception:
        logger.exception("Semantic memory candidate retrieval failed")

    expansion_enabled = settings.graph_memory_expansion_enabled if graph_expansion_enabled is None else graph_expansion_enabled
    expansion_limit = settings.graph_memory_expansion_limit if graph_expansion_limit is None else graph_expansion_limit
    graph_status: Literal["disabled", "no_seeds", "no_candidates", "expanded", "unavailable"] = "disabled"
    seed_ids = list(candidate_ids)
    if expansion_enabled and not seed_ids:
        graph_status = "no_seeds"
    elif expansion_enabled:
        try:
            graph_candidates = graph.expand_memory_candidates(
                seed_memory_ids=seed_ids, owner_id=owner_id, session_id=session_id,
                project_id=project_id, project_scope=project_scope, limit=expansion_limit,
            )
            for graph_candidate in graph_candidates:
                memory_id = graph_candidate.memory_id
                if memory_id in candidate_ids:
                    continue
                record = store.get_owned(memory_id, owner_id=owner_id)
                if record is None or not is_memory_record_eligible(
                    record, owner_id=owner_id, session_id=session_id,
                    project_id=project_id, project_scope=project_scope,
                ):
                    continue
                candidates.append(_Candidate(
                    record, "graph_related", graph_seed_memory_id=graph_candidate.seed_memory_id,
                    graph_relationship_id=graph_candidate.relationship_id,
                    graph_relationship_type=graph_candidate.relationship_type,
                ))
                candidate_ids.add(memory_id)
            graph_status = "expanded" if any(candidate.source == "graph_related" for candidate in candidates) else "no_candidates"
        except Exception:
            logger.exception("Graph memory candidate expansion failed")
            graph_status = "unavailable"

    if not candidates:
        return MemoryContextResult("", [], "no_selection", graph_status=graph_status,
                                   candidate_counts={"exact_identifier": 0, "semantic": 0, "graph_related": 0})

    candidate_counts = {
        source: sum(candidate.source == source for candidate in candidates)
        for source in ("exact_identifier", "semantic", "graph_related")
    }

    try:
        raw_plan = await planner_provider.generate_json(
            _planning_prompt(query, candidates), schema=MemoryPlan.model_json_schema(),
        )
        plan = MemoryPlan.model_validate(raw_plan)
        _validate_plan(plan, candidates)
        result = _render_selected(candidates, plan)
        return MemoryContextResult(**{**result.__dict__, "graph_status": graph_status, "candidate_counts": candidate_counts})
    except Exception as error:
        logger.warning("Memory context planner failed; using exact-match fallback: %s", error)
        result = _render_selected(candidates, None, fallback=True)
        return MemoryContextResult(**{**result.__dict__, "graph_status": graph_status, "candidate_counts": candidate_counts})

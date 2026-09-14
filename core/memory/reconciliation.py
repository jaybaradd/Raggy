"""Constrained post-turn reconciliation of an extracted event and its candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.llm.base import LLMProvider
from core.memory.models import EventMemory, MemoryRecord
from core.memory.projection import memory_text


class ReconciliationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["new", "duplicate", "update", "related", "uncertain"]
    existing_memory_id: str | None = None
    matched_identifier_values: list[str] = Field(default_factory=list, max_length=20)
    changed_claims: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2_000)


@dataclass(frozen=True)
class ReconciliationCandidate:
    """An authoritative candidate and the bounded route by which it was found."""

    record: MemoryRecord
    source: Literal["exact_identifier", "injected_context"]


_IMPLICIT_MATCH_CONFIDENCE = 0.90


class MemoryReconciler:
    """Classify only a bounded, exact-identifier candidate set."""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def reconcile(self, *, incoming: MemoryRecord,
                        candidates: list[ReconciliationCandidate]) -> ReconciliationDecision:
        raw = await self.provider.generate_json(
            self._prompt(incoming, candidates), schema=ReconciliationDecision.model_json_schema(),
        )
        decision = ReconciliationDecision.model_validate(raw)
        self._validate(decision, candidates)
        return decision

    @staticmethod
    def _validate(decision: ReconciliationDecision, candidates: list[ReconciliationCandidate]) -> None:
        candidate_by_id = {candidate.record.memory_id: candidate for candidate in candidates}
        requires_existing = decision.outcome in {"duplicate", "update", "related"}
        if requires_existing and decision.existing_memory_id not in candidate_by_id:
            raise ValueError("reconciliation decision must select a supplied candidate")
        if not requires_existing and decision.existing_memory_id is not None:
            raise ValueError("new or uncertain reconciliation cannot select an existing memory")
        if decision.outcome == "update" and not decision.changed_claims:
            raise ValueError("an update reconciliation must describe changed claims")
        selected = candidate_by_id.get(decision.existing_memory_id or "")
        if selected and selected.source == "injected_context" and decision.confidence < _IMPLICIT_MATCH_CONFIDENCE:
            raise ValueError("implicit context reconciliation requires high confidence")

    @staticmethod
    def _prompt(incoming: MemoryRecord, candidates: list[ReconciliationCandidate]) -> str:
        assert isinstance(incoming.payload, EventMemory)
        identifiers = [reference.normalized_value for reference in incoming.payload.identifier_references]
        candidate_text = "\n".join(
            f"- id={candidate.record.memory_id}; source={candidate.source}; text={memory_text(candidate.record)}"
            for candidate in candidates
        )
        return f"""Classify one incoming operational event against exact identifier candidates.
Event labels are descriptive only: do not require matching labels such as arrival,
delay, reschedule, incident, or meeting. Select an existing ID only from the list.
An `injected_context` candidate is an implicit reference and requires at least
0.90 confidence before selecting it for duplicate, update, or related.

Outcomes: new (unrelated), duplicate (same claims), update (same subject but
material claims differ), related (separate durable event with a useful link),
or uncertain (insufficient confidence). An update never overwrites facts.

Incoming event: {memory_text(incoming)}
Incoming normalized identifiers: {identifiers}
Candidates:
{candidate_text or "(none)"}

Return JSON only with outcome, existing_memory_id, matched_identifier_values,
changed_claims, confidence, and reason.
"""


def reconciliation_details(decision: ReconciliationDecision, incoming: MemoryRecord) -> dict:
    """Build durable, explainable metadata without turning it into a write rule."""
    return {
        "outcome": decision.outcome,
        "matched_identifier_values": decision.matched_identifier_values,
        "changed_claims": decision.changed_claims,
        "reconciliation_confidence": decision.confidence,
        "reconciliation_reason": decision.reason,
        "incoming_source_turn_id": incoming.source_turn_id,
        "incoming_event": incoming.payload.model_dump(mode="json"),
    }

"""Deterministic association of one extracted event to selected memory context."""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.memory.models import EventMemory, MemoryRecord
from core.memory.reconciliation import ReconciliationCandidate

_MIN_PLANNER_CONFIDENCE = 0.90
_STOP_TERMS = {
    "a", "an", "and", "at", "by", "for", "from", "has", "have", "i", "in", "is", "it",
    "my", "of", "on", "the", "their", "this", "to", "user", "with",
}


@dataclass(frozen=True)
class ContextAssociation:
    """One unique, planner-approved contextual update target."""

    record: MemoryRecord
    confidence: float
    matched_subject_terms: tuple[str, ...]

    @property
    def reason(self) -> str:
        terms = ", ".join(self.matched_subject_terms)
        return f"High-confidence selected context matched subject terms: {terms}."


def associate_selected_context(*, incoming: MemoryRecord,
                               candidates: list[ReconciliationCandidate],
                               context_hints: dict[str, dict]) -> ContextAssociation | None:
    """Return one unique context target without using time/status as identity.

    This resolves multi-event correction turns after the prompt planner has
    already declared the relevant prior memories to be updates. A tie or a
    lack of meaningful subject overlap remains intentionally unresolved.
    """
    incoming_entities, incoming_summary = _subject_terms(incoming)
    ranked: list[tuple[int, tuple[str, ...], ReconciliationCandidate, float]] = []
    for candidate in candidates:
        hint = context_hints.get(candidate.record.memory_id, {})
        confidence = float(hint.get("selection_confidence") or 0.0)
        if (candidate.source != "injected_context" or hint.get("selection_relation") != "updates" or
                confidence < _MIN_PLANNER_CONFIDENCE):
            continue
        existing_entities, existing_summary = _subject_terms(candidate.record)
        entity_overlap = incoming_entities & existing_entities
        summary_overlap = incoming_summary & existing_summary
        # Entity overlap is the primary anchor. Summary overlap can strengthen
        # it but cannot independently associate a lifecycle change.
        score = 3 * len(entity_overlap) + len(summary_overlap)
        if entity_overlap:
            ranked.append((score, tuple(sorted(entity_overlap | summary_overlap)), candidate, confidence))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, terms, best, confidence = ranked[0]
    if best_score < 3 or (len(ranked) > 1 and ranked[1][0] == best_score):
        return None
    return ContextAssociation(best.record, confidence, terms)


def _subject_terms(record: MemoryRecord) -> tuple[set[str], set[str]]:
    """Return entity and summary terms, excluding values that often change."""
    assert isinstance(record.payload, EventMemory)
    payload = record.payload
    entity_terms = _terms(" ".join(payload.entities))
    summary_terms = _terms(payload.summary)
    return entity_terms, summary_terms


def _terms(value: str) -> set[str]:
    return {
        term for term in re.findall(r"[a-z][a-z0-9]{1,}", value.casefold())
        if term not in _STOP_TERMS and not term.isdigit()
    }

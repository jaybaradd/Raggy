"""Background jobs for extracting candidate memories after chat turns."""

from __future__ import annotations

import asyncio
import logging

from core.memory.extractor import MemoryExtractor
from core.memory.identity import event_differences
from core.memory.models import MemoryRecord
from core.memory.policy import decide_project_capture
from core.memory.reconciliation import (MemoryReconciler, ReconciliationDecision,
                                        ReconciliationCandidate, reconciliation_details)
from core.memory.projections import sync_pending_projections
from core.retrieval.memory_scope import is_memory_record_eligible
from core.storage.memory_store import MemoryStore

logger = logging.getLogger(__name__)


async def extract_turn_memories(
    *,
    store: MemoryStore,
    extractor: MemoryExtractor,
    source_turn_id: str,
    session_id: str,
    project_scope: str | None,
    project_id: str | None = None,
    owner_id: str,
    user_content: str,
    assistant_content: str,
    evidence_refs: list[str],
    reconciliation_hints: list[dict] | None = None,
) -> int:
    """Extract and persist candidates once for a turn/version pair."""
    if not store.claim_extraction(source_turn_id, extractor.version):
        return 0
    try:
        batch = await extractor.extract_turn(
            session_id=session_id,
            user_content=user_content,
            assistant_content=assistant_content,
            evidence_refs=evidence_refs,
        )
        for candidate in batch.candidates:
            decision = decide_project_capture(
                candidate=candidate,
                project_scope=project_scope,
                user_content=user_content,
            )
            record = MemoryRecord(
                owner_id=owner_id,
                scope=decision.scope,
                session_id=session_id,
                project_id=project_id,
                project_scope=project_scope,
                kind=candidate.kind,
                status=decision.status,
                user_confirmed=decision.user_confirmed,
                confidence=candidate.confidence,
                evidence_refs=candidate.evidence_refs,
                source_turn_id=source_turn_id,
                extraction_model=extractor.provider.__class__.__name__,
                extraction_version=extractor.version,
                valid_to=decision.valid_to,
                payload=candidate.payload,
            )
            if candidate.kind == "event":
                await _reconcile_event(
                    store=store, record=record, provider=extractor.provider,
                    policy_reason=decision.reason, reconciliation_hints=reconciliation_hints or [],
                )
            else:
                store.upsert(record, event_type=decision.event_type,
                             details={"policy_reason": decision.reason})
        # Projection jobs make active project records searchable and graph
        # compatible, while candidates remain deliberately absent from both.
        await asyncio.to_thread(sync_pending_projections, store=store)
        store.complete_extraction(source_turn_id, extractor.version, status="completed")
        return len(batch.candidates)
    except Exception as exc:
        store.complete_extraction(source_turn_id, extractor.version, status="failed", error=str(exc))
        logger.exception("Memory extraction failed for turn %s", source_turn_id)
        return 0


async def _reconcile_event(*, store, record: MemoryRecord, provider, policy_reason: str,
                           reconciliation_hints: list[dict] | None = None) -> None:
    """Reconcile exact identifiers plus selected context from this same turn.

    A failed model call is deliberately non-destructive: the event remains a
    reviewable candidate whenever existing exact identifiers made reconciliation
    necessary.
    """
    identifiers = record.payload.identifier_references
    exact_records = store.find_memory_candidates(
        owner_id=record.owner_id, session_id=record.session_id or "",
        project_id=record.project_id, project_scope=record.project_scope,
        identifier_references=identifiers,
    ) if identifiers else []
    candidates = [ReconciliationCandidate(item, "exact_identifier") for item in exact_records]
    known_ids = {candidate.record.memory_id for candidate in candidates}
    context_hints: dict[str, dict] = {}
    for hint in reconciliation_hints or []:
        memory_id = str(hint.get("memory_id", ""))
        if not memory_id or memory_id in known_ids:
            continue
        context_record = store.get_owned(memory_id, owner_id=record.owner_id)
        if (context_record is None or context_record.kind != "event" or
                not is_memory_record_eligible(
                    context_record, owner_id=record.owner_id, session_id=record.session_id or "",
                    project_id=record.project_id, project_scope=record.project_scope,
                )):
            continue
        candidates.append(ReconciliationCandidate(context_record, "injected_context"))
        known_ids.add(memory_id)
        context_hints[memory_id] = hint
    if not candidates:
        decision = ReconciliationDecision(
            outcome="new", confidence=1.0, reason="No exact identifier candidate was available.",
        )
    else:
        deterministic_update = _selected_context_update(record, candidates, context_hints)
        if deterministic_update is not None:
            decision = deterministic_update
        else:
            try:
                decision = await MemoryReconciler(provider).reconcile(incoming=record, candidates=candidates)
            except Exception as error:
                logger.warning("Memory reconciliation failed for turn %s: %s", record.source_turn_id, error)
                decision = ReconciliationDecision(
                    outcome="uncertain", confidence=0.0,
                    reason="Reconciliation planner was unavailable; retained for review.",
                )
    details = {"policy_reason": policy_reason, **reconciliation_details(decision, record)}
    store.apply_event_reconciliation(
        record, outcome=decision.outcome, existing_memory_id=decision.existing_memory_id,
        details=details, actor_id="system",
    )


def _selected_context_update(record: MemoryRecord, candidates: list[ReconciliationCandidate],
                             context_hints: dict[str, dict]) -> ReconciliationDecision | None:
    """Convert one already-approved contextual update into a review conflict.

    The pre-response planner has already selected this exact scoped memory for
    the current turn and declared it an update. Requiring another model call
    would add failure risk without adding authority; the repository still
    requires user review before it changes the old active fact.
    """
    matches = [
        candidate for candidate in candidates
        if candidate.source == "injected_context"
        and context_hints.get(candidate.record.memory_id, {}).get("selection_relation") == "updates"
        and float(context_hints[candidate.record.memory_id].get("selection_confidence") or 0.0) >= 0.90
    ]
    if len(matches) != 1:
        return None
    existing = matches[0].record
    changed_claims = event_differences(existing, record)
    if not changed_claims:
        return None
    hint = context_hints[existing.memory_id]
    return ReconciliationDecision(
        outcome="update", existing_memory_id=existing.memory_id,
        changed_claims=changed_claims,
        confidence=float(hint["selection_confidence"]),
        reason="High-confidence selected memory context identified this turn as an update.",
    )

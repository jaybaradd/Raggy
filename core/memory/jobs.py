"""Background jobs for extracting candidate memories after chat turns."""

from __future__ import annotations

import asyncio
import logging

from core.memory.extractor import MemoryExtractor
from core.memory.models import MemoryRecord
from core.memory.policy import decide_project_capture
from core.memory.projections import sync_pending_projections
from core.storage.memory_store import MemoryStore

logger = logging.getLogger(__name__)


async def extract_turn_memories(
    *,
    store: MemoryStore,
    extractor: MemoryExtractor,
    source_turn_id: str,
    session_id: str,
    project_scope: str | None,
    owner_id: str,
    user_content: str,
    assistant_content: str,
    evidence_refs: list[str],
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
                store.capture_event(record, event_type=decision.event_type,
                                    details={"policy_reason": decision.reason})
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

"""Rebuildable evidence-to-Qdrant projection worker."""
from __future__ import annotations
import logging
from core.ingestion.models import EvidenceSegment
from core.ingestion.parser import ParsedChunk

logger = logging.getLogger(__name__)

def _chunk(segment: EvidenceSegment) -> ParsedChunk:
    modality = {"video": "video_segment", "audio": "audio_segment"}.get(segment.modality, segment.modality)
    metadata = segment.retrieval_metadata
    return ParsedChunk(chunk_id=segment.evidence_id, evidence_id=segment.evidence_id, doc_id=segment.asset_id,
                       modality=modality, representation=segment.representation, content=segment.content or "",
                       context_prefix=metadata.get("context_prefix"), source_locator=segment.locator,
                       raw_file_uri=segment.media_uri or "", source_name=segment.source_name,
                       parser_backend=segment.parser_backend, embedding_model=metadata.get("embedding_model") or "",
                       created_at=segment.created_at)

def sync_pending_evidence_projections(*, store, qdrant=None, embedder_client=None,
                                      limit: int = 100) -> dict[str, int]:
    """Project durable evidence records to Qdrant.

    The embedding dependency is loaded only when work exists.  Supplying it
    explicitly keeps this small worker deterministic in tests and reusable by
    future background-worker processes.
    """
    if qdrant is None:
        from core.storage.qdrant_store import qdrant_store
        qdrant = qdrant_store
    completed = failed = skipped = 0
    for job in store.claim_projection_jobs(limit=limit):
        segment = store.get_evidence_for_projection(job["evidence_id"])
        if segment is None:
            store.fail_projection_job(job["job_id"], "evidence segment no longer exists"); failed += 1; continue
        if not (segment.content or "").strip():
            store.complete_projection_job(job["job_id"]); completed += 1; skipped += 1; continue
        try:
            if embedder_client is None:
                from core.embeddings import embedder as default_embedder
                embedder_client = default_embedder
            chunk = _chunk(segment); qdrant.upsert([chunk], embedder_client.encode([chunk.content]))
        except Exception as exc:
            logger.exception("Evidence projection failed for %s", segment.evidence_id)
            store.fail_projection_job(job["job_id"], str(exc)); failed += 1
        else:
            store.complete_projection_job(job["job_id"]); completed += 1
    return {"completed": completed, "failed": failed, "skipped": skipped}

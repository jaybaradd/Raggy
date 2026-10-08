"""Rebuildable evidence-to-Qdrant projection worker."""
from __future__ import annotations
import logging
from core.ingestion.models import EvidenceSegment
from core.ingestion.parser import ParsedChunk
from core.latency import elapsed_ms, log_latency, now_ns

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
                                      limit: int = 100, binding_id: int | None = None,
                                      run_id: str | None = None) -> dict[str, int]:
    """Project durable evidence records to Qdrant.

    The embedding dependency is loaded only when work exists.  Supplying it
    explicitly keeps this small worker deterministic in tests and reusable by
    future background-worker processes.
    """
    if qdrant is None:
        from core.storage.qdrant_store import qdrant_store
        qdrant = qdrant_store
    total_started = now_ns()
    claim_started = now_ns()
    jobs = store.claim_projection_jobs(limit=limit, binding_id=binding_id)
    log_latency(
        "projection", "claim_jobs", elapsed_ms(claim_started), run_id=run_id,
        binding_id=binding_id, job_count=len(jobs),
    )
    completed = failed = skipped = 0
    input_ms = embedding_ms = qdrant_ms = completion_ms = 0.0
    for job in jobs:
        started = now_ns()
        projection = store.get_projection_input(job["evidence_id"], binding_id=job["binding_id"])
        input_ms += elapsed_ms(started)
        if projection is None:
            store.fail_projection_job(job["job_id"], "evidence segment no longer exists"); failed += 1; continue
        segment = projection["segment"]
        if not (segment.content or "").strip():
            store.complete_projection_job(job["job_id"]); completed += 1; skipped += 1; continue
        try:
            if embedder_client is None:
                from core.embeddings import embedder as default_embedder
                embedder_client = default_embedder
            chunk = _chunk(segment)
            started = now_ns()
            vector = embedder_client.encode([chunk.content])
            embedding_ms += elapsed_ms(started)
            started = now_ns()
            qdrant.upsert([chunk], vector, binding=projection)
            qdrant_ms += elapsed_ms(started)
        except Exception as exc:
            logger.exception("Evidence projection failed for %s", segment.evidence_id)
            store.fail_projection_job(job["job_id"], str(exc)); failed += 1
        else:
            started = now_ns()
            store.complete_projection_job(job["job_id"])
            completion_ms += elapsed_ms(started)
            completed += 1
    common = {"run_id": run_id, "binding_id": binding_id, "job_count": len(jobs)}
    log_latency("projection", "load_inputs", input_ms, **common)
    log_latency("projection", "dense_embedding", embedding_ms, **common)
    log_latency("projection", "qdrant_upsert", qdrant_ms, **common)
    log_latency("projection", "complete_jobs", completion_ms, **common)
    log_latency(
        "projection", "total", elapsed_ms(total_started), completed=completed,
        failed=failed, skipped=skipped, **common,
    )
    return {"completed": completed, "failed": failed, "skipped": skipped}

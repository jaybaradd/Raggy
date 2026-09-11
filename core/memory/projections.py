"""Durable synchronization of memory projections.

Authoritative memory writes enqueue work in SQLite. This module performs the
best-effort projection writes and leaves failed jobs inspectable/retryable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.storage.graph_store import GraphStore
from core.storage.memory_store import MemoryStore

if TYPE_CHECKING:
    from core.storage.qdrant_store import QdrantStore

logger = logging.getLogger(__name__)


def sync_pending_projections(*, store: MemoryStore,
                             graph: GraphStore | None = None,
                             qdrant: "QdrantStore | None" = None,
                             limit: int = 100) -> dict[str, int]:
    """Synchronize queued projection jobs without changing authoritative state."""
    if graph is None:
        from core.storage.graph_store import graph_store
        graph = graph_store
    if qdrant is None:
        from core.storage.qdrant_store import qdrant_store
        qdrant = qdrant_store
    completed = failed = 0
    for job in store.claim_projection_jobs(limit=limit):
        record = store.get(job["memory_id"])
        if record is None:
            store.fail_projection_job(job["job_id"], "memory record no longer exists")
            failed += 1
            continue
        try:
            if job["target"] == "qdrant":
                qdrant.upsert_memory(record)
            elif job["target"] == "graph":
                graph.sync_memory(record)
            else:
                raise ValueError(f"unknown projection target: {job['target']}")
        except Exception as exc:
            logger.exception("Memory %s projection failed for %s", job["target"], record.memory_id)
            store.fail_projection_job(job["job_id"], str(exc))
            failed += 1
        else:
            store.complete_projection_job(job["job_id"])
            completed += 1
    return {"completed": completed, "failed": failed}

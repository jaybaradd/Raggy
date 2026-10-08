"""Durable synchronization of memory projections.

Authoritative memory writes enqueue durable work. This module performs the
best-effort projection writes and leaves failed jobs inspectable/retryable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.storage.memory_store import MemoryStore

if TYPE_CHECKING:
    from core.storage.qdrant_store import QdrantStore
    from db.repositories import GraphRepository

logger = logging.getLogger(__name__)


def sync_pending_projections(*, store: MemoryStore,
                             graph: "GraphRepository",
                             qdrant: "QdrantStore | None" = None,
                             limit: int = 100,
                             targets: set[str] | None = None) -> dict[str, int]:
    """Synchronize queued jobs using the graph repository selected by the app.

    The graph is deliberately required: callers must never silently write to a
    module-level SQLite graph when another graph repository is configured.
    """
    completed = failed = 0
    for job in store.claim_projection_jobs(limit=limit, targets=targets):
        record = store.get(job["memory_id"])
        if record is None:
            store.fail_projection_job(job["job_id"], "memory record no longer exists")
            failed += 1
            continue
        try:
            if job["target"] == "qdrant":
                if qdrant is None:
                    # Keep graph-only repair tooling independent from the
                    # embedding model and local Qdrant initialization.
                    from core.storage.qdrant_store import qdrant_store
                    qdrant = qdrant_store
                qdrant.upsert_memory(record)
            elif job["target"] == "graph":
                graph.sync_memory(record)
                for relationship in store.list_graph_relationships(
                    owner_id=record.owner_id, memory_id=record.memory_id,
                ):
                    graph.sync_relationship(relationship)
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
